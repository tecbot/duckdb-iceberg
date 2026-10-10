#include "common/iceberg_snapshot_properties.hpp"

#include "core/metadata/snapshot/iceberg_snapshot.hpp"
#include "catalog/rest/api/catalog_utils.hpp"
#include "duckdb/common/exception.hpp"
#include "yyjson.hpp"
#include "duckdb/main/client_context.hpp"
#include "iceberg_options.hpp"

namespace duckdb {

void IcebergSnapshotProperties::ValidateKey(const string &key) {
	if (key == "operation" || IcebergSnapshotMetrics::IsMetricKey(key)) {
		throw InvalidConfigurationException("'%s' property '%s' is managed by the Iceberg extension",
		                                    SNAPSHOT_PROPERTIES_CONFIG_VARIABLE, key);
	}
}

unordered_map<string, string> IcebergSnapshotProperties::Parse(const Value &parameter) {
	if (parameter.IsNull()) {
		throw InvalidConfigurationException("'%s' must be a JSON object with string values, not NULL",
		                                    SNAPSHOT_PROPERTIES_CONFIG_VARIABLE);
	}
	auto &text = StringValue::Get(parameter);
	auto document = std::unique_ptr<yyjson_doc, YyjsonDocDeleter>(yyjson_read(text.data(), text.size(), 0));
	if (!document || !yyjson_is_obj(yyjson_doc_get_root(document.get()))) {
		throw InvalidConfigurationException("'%s' must be a JSON object with string values",
		                                    SNAPSHOT_PROPERTIES_CONFIG_VARIABLE);
	}
	unordered_map<string, string> properties;
	size_t idx, max;
	yyjson_val *json_key, *value;
	yyjson_obj_foreach(yyjson_doc_get_root(document.get()), idx, max, json_key, value) {
		string key(yyjson_get_str(json_key), yyjson_get_len(json_key));
		ValidateKey(key);
		if (!yyjson_is_str(value)) {
			throw InvalidConfigurationException("'%s' property '%s' must have a string value",
			                                    SNAPSHOT_PROPERTIES_CONFIG_VARIABLE, key);
		}
		if (!properties.emplace(key, string(yyjson_get_str(value), yyjson_get_len(value))).second) {
			throw InvalidConfigurationException("'%s' has duplicate property '%s'", SNAPSHOT_PROPERTIES_CONFIG_VARIABLE,
			                                    key);
		}
	}
	return properties;
}

void IcebergSnapshotProperties::Set(ClientContext &context, SetScope scope, Value &parameter) {
	if (scope == SetScope::GLOBAL) {
		throw InvalidConfigurationException("'%s' can only be set per session", SNAPSHOT_PROPERTIES_CONFIG_VARIABLE);
	}
	Parse(parameter);
}

unordered_map<string, string> IcebergSnapshotProperties::Get(ClientContext &context) {
	Value parameter;
	if (!context.TryGetCurrentSetting(SNAPSHOT_PROPERTIES_CONFIG_VARIABLE, parameter)) {
		throw InternalException("Iceberg snapshot properties setting is not registered");
	}
	return Parse(parameter);
}

} // namespace duckdb
