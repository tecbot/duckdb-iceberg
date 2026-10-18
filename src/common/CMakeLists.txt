add_library(iceberg_common OBJECT iceberg_default.cpp iceberg_fixed_type.cpp
                                  iceberg_utils.cpp iceberg_snapshot_properties.cpp)
set(ALL_OBJECT_FILES
    ${ALL_OBJECT_FILES} $<TARGET_OBJECTS:iceberg_common>
    PARENT_SCOPE)
