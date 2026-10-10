#pragma once

#include "duckdb/common/enums/set_scope.hpp"
#include "duckdb/common/string.hpp"
#include "duckdb/common/unordered_map.hpp"
#include "duckdb/common/types/value.hpp"

namespace duckdb {

class ClientContext;

class IcebergSnapshotProperties {
public:
	static void Set(ClientContext &context, SetScope scope, Value &parameter);
	static unordered_map<string, string> Get(ClientContext &context);
	static void ValidateKey(const string &key);

private:
	static unordered_map<string, string> Parse(const Value &parameter);
};

} // namespace duckdb
