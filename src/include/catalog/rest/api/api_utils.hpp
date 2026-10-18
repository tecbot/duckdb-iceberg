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

// we statically compile in libcurl, which means the cert file location of the build machine is the
// place curl will look. But not every distro has this file in the same location, so we search a
// number of common locations and use the first one we find.
static string certFileLocations[] = {
    // Arch, Debian-based, Gentoo
    "/etc/ssl/certs/ca-certificates.crt",
    // RedHat 7 based
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
    // Redhat 6 based
    "/etc/pki/tls/certs/ca-bundle.crt",
    // OpenSUSE
    "/etc/ssl/ca-bundle.pem",
    // Alpine
    "/etc/ssl/cert.pem"};

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
	static unique_ptr<HTTPResponse> Request(RequestType request_type, optional_ptr<AttachedDatabase> db,
	                                        ClientContext &context, const IRCEndpointBuilder &endpoint_builder,
	                                        HTTPHeaders &headers, const string &data,
	                                        optional_ptr<IcebergMutationRequest> mutation = nullptr);
	static int64_t ParseIdempotencyKeyLifetime(const string &lifetime);
	static unique_ptr<HTTPResponse> RequestMutation(RequestType request_type, IcebergCatalog &catalog,
	                                                ClientContext &context, const IRCEndpointBuilder &endpoint_builder,
	                                                HTTPHeaders &headers, const string &data = "");

	//! We use a singleton here to store the path, set by SelectCurlCertPath
	static const string &GetCURLCertPath();
};

} // namespace duckdb
