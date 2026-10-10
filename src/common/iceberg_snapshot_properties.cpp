#include "common/iceberg_snapshot_properties.hpp"

#include "core/metadata/snapshot/iceberg_snapshot_metrics.hpp"
#include "duckdb/common/exception.hpp"
#include "duckdb/common/json_document.hpp"
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
	JSONParseError error;
	auto document = JSONDocument::TryParse(text.data(), text.size(), error);
	if (!document || !document->GetRoot().IsObject()) {
		throw InvalidConfigurationException("'%s' must be a JSON object with string values",
		                                    SNAPSHOT_PROPERTIES_CONFIG_VARIABLE);
	}
	unordered_map<string, string> properties;
	document->GetRoot().IterateObject([&](const string &key, JSONValue value) {
		ValidateKey(key);
		if (!value.IsString()) {
			throw InvalidConfigurationException("'%s' property '%s' must have a string value",
			                                    SNAPSHOT_PROPERTIES_CONFIG_VARIABLE, key);
		}
		if (!properties.emplace(key, value.GetString()).second) {
			throw InvalidConfigurationException("'%s' has duplicate property '%s'", SNAPSHOT_PROPERTIES_CONFIG_VARIABLE,
			                                    key);
		}
	});
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
