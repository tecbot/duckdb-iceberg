#include "catalog/rest/api/api_utils.hpp"

#include "duckdb/common/exception.hpp"
#include "duckdb/common/http_util.hpp"
#include "duckdb/main/extension_helper.hpp"
#include "duckdb/common/exception/http_exception.hpp"
#include "duckdb/common/string_util.hpp"
#include "duckdb/main/client_data.hpp"
#include "duckdb/main/database.hpp"
#include "duckdb/main/extension/extension_loader.hpp"

#include <sys/stat.h>
#include <cmath>
#include <thread>
#include <regex>

#include "duckdb/common/types/uuid.hpp"
#include "duckdb/common/types/interval.hpp"
#include "catalog/rest/iceberg_catalog.hpp"

namespace duckdb {
IcebergMutationRequest::IcebergMutationRequest(int64_t lifetime_micros)
    : key(UUIDv7::ToString(UUIDv7::GenerateRandomUUID())),
      deadline(std::chrono::steady_clock::now() + std::chrono::microseconds(lifetime_micros)) {
}

void IcebergMutationRequest::CheckLifetime() const {
	if (std::chrono::steady_clock::now() >= deadline) {
		throw IOException("Idempotency-Key lifetime elapsed; catalog mutation outcome is unknown. "
		                  "Check the catalog state before submitting another mutation.");
	}
}

void IcebergMutationRequest::Prepare(HTTPHeaders &headers, HTTPParams &params) const {
	CheckLifetime();
	headers["Idempotency-Key"] = key;
	// The catalog request owns retries so it can check the lifetime before every submission, including OAuth refresh.
	params.retries = 0;
}

void IcebergMutationRequest::WaitBeforeRetry(idx_t retry, const HTTPParams &params) const {
	CheckLifetime();
	auto delay = retry == 0 ? 0.0 : double(params.retry_wait_ms) * std::pow(params.retry_backoff, double(retry - 1));
	auto remaining = std::chrono::duration<double, std::milli>(deadline - std::chrono::steady_clock::now()).count();
	if (!std::isfinite(delay) || delay < 0 || delay >= remaining) {
		throw IOException(
		    "Idempotency-Key lifetime does not permit another retry; catalog mutation outcome is unknown.");
	}
#ifndef DUCKDB_NO_THREADS
	std::this_thread::sleep_for(std::chrono::duration<double, std::milli>(delay));
#endif
	CheckLifetime();
}

int64_t APIUtils::ParseIdempotencyKeyLifetime(const string &lifetime) {
	// Catalog retention is a duration, so calendar months and years have no fixed retry window.
	static const std::regex pattern(
	    "^P(?:([0-9]+)D)?(?:T(?:([0-9]+)H)?(?:([0-9]+)M)?(?:([0-9]+)(?:[.]([0-9]{1,6}))?S)?)?$");
	std::smatch match;
	auto invalid = []() {
		throw InvalidConfigurationException(
		    "Invalid catalog idempotency-key-lifetime: expected a positive ISO-8601 duration");
	};
	if (!std::regex_match(lifetime, match, pattern) || lifetime.back() == 'P' || lifetime.back() == 'T') {
		invalid();
	}
	const int64_t multipliers[] = {Interval::MICROS_PER_DAY, Interval::MICROS_PER_HOUR, Interval::MICROS_PER_MINUTE,
	                               Interval::MICROS_PER_SEC};
	int64_t micros = 0;
	for (idx_t i = 0; i < 4; i++) {
		if (!match[i + 1].matched) {
			continue;
		}
		int64_t value;
		try {
			value = std::stoll(match[i + 1].str());
		} catch (const std::exception &) {
			invalid();
			return 0;
		}
		if (value > (NumericLimits<int64_t>::Maximum() - micros) / multipliers[i]) {
			invalid();
		}
		micros += value * multipliers[i];
	}
	if (match[5].matched) {
		auto fraction = match[5].str();
		fraction.append(6 - fraction.size(), '0');
		auto value = std::stoll(fraction);
		if (micros > NumericLimits<int64_t>::Maximum() - value) {
			invalid();
		}
		micros += value;
	}
	if (micros <= 0 || micros > std::chrono::duration_cast<std::chrono::microseconds>(
	                                std::chrono::steady_clock::time_point::max() - std::chrono::steady_clock::now())
	                                .count()) {
		invalid();
	}
	return micros;
}

unique_ptr<HTTPResponse> APIUtils::RequestMutation(RequestType request_type, IcebergCatalog &catalog,
                                                   ClientContext &context, const IRCEndpointBuilder &endpoint_builder,
                                                   HTTPHeaders &headers, const string &data) {
	if (!catalog.idempotency_key_lifetime_micros.IsValid()) {
		return catalog.auth_handler->Request(request_type, context, endpoint_builder, headers, data);
	}
	ExtensionHelper::AutoLoadExtension(*context.db, "httpfs");
	auto &http_util = HTTPUtil::Get(*context.db);
	auto params = http_util.InitializeParameters(context, AddHttpHostIfMissing(endpoint_builder.GetURLEncoded()));
	IcebergMutationRequest mutation(catalog.idempotency_key_lifetime_micros.GetIndex());
	for (idx_t attempt = 0;; attempt++) {
		mutation.CheckLifetime();
		headers["Idempotency-Key"] = mutation.key;
		unique_ptr<HTTPResponse> response;
		std::exception_ptr transport_error;
		try {
			response = catalog.auth_handler->Request(request_type, context, endpoint_builder, headers, data, mutation);
		} catch (IOException &) {
			transport_error = std::current_exception();
		} catch (HTTPException &) {
			transport_error = std::current_exception();
		}
		if (response && !response->ShouldRetry() && response->status != HTTPStatusCode::BadGateway_502) {
			return response;
		}
		if (attempt >= params->retries) {
			if (transport_error) {
				std::rethrow_exception(transport_error);
			}
			if (!response || response->HasRequestError()) {
				throw IOException("Catalog mutation transport failed; outcome is unknown: %s",
				                  response ? response->GetRequestError() : "no response");
			}
			return response;
		}
		mutation.WaitBeforeRetry(attempt, *params);
	}
}

//! Grab the first path that exists, from a list of well-known locations
static string SelectCURLCertPath() {
	for (string &caFile : certFileLocations) {
		struct stat buf;
		if (stat(caFile.c_str(), &buf) == 0) {
			return caFile;
		}
	}
	return string();
}

const string &APIUtils::GetCURLCertPath() {
	static string cert_path = SelectCURLCertPath();
	return cert_path;
}

unique_ptr<HTTPResponse> APIUtils::Request(RequestType request_type, optional_ptr<AttachedDatabase> attached_db,
                                           ClientContext &context, const IRCEndpointBuilder &endpoint_builder,
                                           HTTPHeaders &headers, const string &data,
                                           optional_ptr<IcebergMutationRequest> mutation) {
	// load httpfs since iceberg requests do not go through the file system api
	if (!context.db.get()) {
		throw InvalidConfigurationException("Context does not have database instance when loading Httpfs in Iceberg");
	}
	ExtensionHelper::AutoLoadExtension(*context.db, "httpfs");
	if (!context.db->ExtensionIsLoaded("httpfs")) {
		throw MissingExtensionException("The iceberg extension requires the httpfs extension to be loaded!");
	}

	auto &db = DatabaseInstance::GetDatabase(context);
	string request_url = AddHttpHostIfMissing(endpoint_builder.GetURLEncoded());

	auto &http_util = HTTPUtil::Get(db);
	unique_ptr<HTTPParams> params;
	params = http_util.InitializeParameters(context, request_url);

	if (mutation) {
		mutation->Prepare(headers, *params);
	}

	unique_ptr<HTTPClient> placeholder_client;
	auto &client =
	    attached_db ? IcebergAuthorizationContextState::GetHTTPClient(*attached_db, context) : placeholder_client;
	if (client) {
		client->Initialize(*params);
	}

	switch (request_type) {
	case RequestType::GET_REQUEST: {
		GetRequestInfo get_request(request_url, headers, *params, nullptr, nullptr);
		return http_util.Request(get_request, client);
	}
	case RequestType::DELETE_REQUEST: {
		DeleteRequestInfo delete_request(request_url, headers, *params);
		delete_request.try_request = bool(mutation);
		return http_util.Request(delete_request, client);
	}
	case RequestType::POST_REQUEST: {
		PostRequestInfo post_request(request_url, headers, *params, reinterpret_cast<const_data_ptr_t>(data.data()),
		                             data.size());
		post_request.try_request = bool(mutation);
		auto response = http_util.Request(post_request, client);
		response->body = post_request.buffer_out;
		return response;
	}
	case RequestType::HEAD_REQUEST: {
		HeadRequestInfo head_request(request_url, headers, *params);
		return http_util.Request(head_request, client);
	}
	default:
		throw NotImplementedException("Cannot make request of type %s", EnumUtil::ToString(request_type));
	}
}

} // namespace duckdb
