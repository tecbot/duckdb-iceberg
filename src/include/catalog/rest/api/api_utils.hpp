//===----------------------------------------------------------------------===//
//                         DuckDB
//
// api_utils.hpp
//
//
//===----------------------------------------------------------------------===//

#pragma once

#include <chrono>

#include "duckdb/common/printer.hpp"
#include "duckdb/common/file_system.hpp"
#include "duckdb/common/http_util.hpp"

#include "catalog/rest/api/url_utils.hpp"
#include "catalog/rest/storage/aws.hpp"
#include "catalog/rest/storage/iceberg_authorization.hpp"

namespace duckdb {

class IcebergCatalog;

class IcebergMutationRequest {
public:
	explicit IcebergMutationRequest(int64_t lifetime_micros);
	void CheckLifetime() const;
	void Prepare(HTTPHeaders &headers, HTTPParams &params) const;
	void WaitBeforeRetry(idx_t retry, const HTTPParams &params) const;
	const string key;

private:
	const std::chrono::steady_clock::time_point deadline;
};

class APIUtils {
public:
	static unique_ptr<HTTPResponse> Request(RequestType request_type, ClientContext &context,
	                                        const IRCEndpointBuilder &endpoint_builder, HTTPHeaders &headers,
	                                        const string &data,
	                                        optional_ptr<IcebergMutationRequest> mutation = nullptr);
	static int64_t ParseIdempotencyKeyLifetime(const string &lifetime);
	static unique_ptr<HTTPResponse> RequestMutation(RequestType request_type, IcebergCatalog &catalog,
	                                                ClientContext &context, const IRCEndpointBuilder &endpoint_builder,
	                                                HTTPHeaders &headers, const string &data = "");
};

} // namespace duckdb
