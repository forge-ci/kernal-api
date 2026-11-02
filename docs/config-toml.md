# Bounded TOML configuration

Enable `config-toml` for `config::Document::parse_toml(&str)`. The document's
root is a kernel-owned `Value::Table`; children preserve strings, signed
integers, floating-point values, booleans, arrays, tables, and canonical TOML
date/time text. No parser types, Serde traits, filesystem reads, interpolation,
or application defaults are exposed. Comments and original formatting are not
retained. Applications match values and enforce their own field schemas.

Source is limited to 1 MiB before parsing. The returned document is limited to
16,384 values (including containers and the root) and depth 32 (root depth 0).
Those two decoded limits are checked after backend parsing, not as independent
CPU or parser-allocation quotas. The private parser retains its default
recursion limit. Errors return no partial document and do not echo source text.

The exact private `toml` 1.1.8 dependency is enabled on runtime edges only by
this feature. It already occurs as a build dependency, so whole dependency-graph
absence and build-speed gains are not claimed. CI checks runtime edges separately.
