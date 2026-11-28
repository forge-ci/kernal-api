//! Configuration mechanics only; applications own field validation and defaults.

use std::collections::BTreeMap;

/// Maximum UTF-8 source bytes, checked before invoking the parser.
pub const MAX_INPUT_BYTES: usize = 1024 * 1024;
/// Maximum decoded values, counting the root table and every container.
pub const MAX_NODES: usize = 16_384;
/// Maximum value depth, with the root table at depth zero.
pub const MAX_DEPTH: usize = 32;

/// Semantic configuration values independent of the private TOML parser.
/// Table keys are ordered; source formatting and comments are not retained.
#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    String(String),
    Integer(i64),
    Float(f64),
    Boolean(bool),
    /// Canonical TOML date/time spelling; no timezone conversion is performed.
    DateTime(String),
    Array(Vec<Value>),
    Table(BTreeMap<String, Value>),
}

/// A parsed configuration. Construction enforces bounds before it is returned.
#[derive(Clone, Debug, PartialEq)]
pub struct Document {
    root: Value,
}

/// Bounded diagnostics that never echo configuration contents or secrets.
#[derive(Clone, Copy, Debug, Eq, PartialEq, thiserror::Error)]
pub enum ParseError {
    #[error("configuration exceeds 1048576 UTF-8 source bytes")]
    InputTooLarge,
    #[error("invalid TOML document")]
    InvalidSyntax,
    #[error("configuration exceeds 16384 decoded values")]
    TooManyNodes,
    #[error("configuration exceeds value depth 32")]
    TooDeep,
}

impl Document {
    /// Decode a TOML document without filesystem access or interpolation.
    ///
    /// The source byte bound applies before parsing. Node/depth bounds apply
    /// after the private parser constructs its tree, during conversion to owned
    /// semantic values. They are not independent parser CPU or allocation quotas;
    /// source size and the parser's own recursion limit bound that earlier stage.
    /// No partial document is returned on error. Unknown fields remain values
    /// for application policy; empty input is a valid empty table.
    pub fn parse_toml(source: &str) -> Result<Self, ParseError> {
        if source.len() > MAX_INPUT_BYTES {
            return Err(ParseError::InputTooLarge);
        }
        let table = toml::from_str::<toml::Table>(source).map_err(|_| ParseError::InvalidSyntax)?;
        let mut remaining = MAX_NODES;
        Ok(Self {
            root: convert(toml::Value::Table(table), 0, &mut remaining)?,
        })
    }

    /// The root of a TOML document is always a table.
    pub fn root(&self) -> &Value {
        &self.root
    }
}

/// Serialization failures; diagnostics never echo configuration contents.
#[derive(Clone, Copy, Debug, Eq, PartialEq, thiserror::Error)]
pub enum SerializeError {
    #[error("value is not a valid TOML date/time")]
    InvalidDateTime,
    #[error("configuration exceeds value depth 32")]
    TooDeep,
    #[error("TOML document root must be a table")]
    RootNotTable,
    #[error("value cannot be encoded as TOML")]
    Unencodable,
}

/// Encode `text` as one TOML basic or literal string, including its quotes.
///
/// The output parses back to exactly `text` as a TOML string value, so it can
/// be spliced into generated TOML (for example `key = <quoted>`).
pub fn quote_toml_string(text: &str) -> String {
    toml::Value::String(text.to_owned()).to_string()
}

impl Value {
    /// Encode this value as one TOML value expression (tables become inline
    /// tables), suitable for the right-hand side of `key = ...`.
    ///
    /// # Errors
    /// `InvalidDateTime` for a `DateTime` that is not canonical TOML, and
    /// `TooDeep` beyond [`MAX_DEPTH`].
    pub fn to_toml_value(&self) -> Result<String, SerializeError> {
        Ok(to_backend(self, 0)?.to_string())
    }

    /// Encode a `Table` as a complete TOML document, with nested tables as
    /// `[section]` headers. Parsing the output with [`Document::parse_toml`]
    /// yields an equal value.
    ///
    /// # Errors
    /// `RootNotTable` unless this is a table, plus the
    /// [`Self::to_toml_value`] failures.
    pub fn to_toml_document(&self) -> Result<String, SerializeError> {
        let toml::Value::Table(table) = to_backend(self, 0)? else {
            return Err(SerializeError::RootNotTable);
        };
        toml::to_string(&table).map_err(|_| SerializeError::Unencodable)
    }
}

impl Document {
    /// Encode this document as TOML text; comments and layout are not kept.
    ///
    /// # Errors
    /// See [`Value::to_toml_document`].
    pub fn to_toml_string(&self) -> Result<String, SerializeError> {
        self.root.to_toml_document()
    }
}

fn to_backend(value: &Value, depth: usize) -> Result<toml::Value, SerializeError> {
    if depth > MAX_DEPTH {
        return Err(SerializeError::TooDeep);
    }
    Ok(match value {
        Value::String(value) => toml::Value::String(value.clone()),
        Value::Integer(value) => toml::Value::Integer(*value),
        Value::Float(value) => toml::Value::Float(*value),
        Value::Boolean(value) => toml::Value::Boolean(*value),
        Value::DateTime(value) => {
            toml::Value::Datetime(value.parse().map_err(|_| SerializeError::InvalidDateTime)?)
        }
        Value::Array(values) => toml::Value::Array(
            values
                .iter()
                .map(|value| to_backend(value, depth + 1))
                .collect::<Result<_, _>>()?,
        ),
        Value::Table(values) => toml::Value::Table(
            values
                .iter()
                .map(|(key, value)| Ok((key.clone(), to_backend(value, depth + 1)?)))
                .collect::<Result<_, SerializeError>>()?,
        ),
    })
}

fn convert(value: toml::Value, depth: usize, remaining: &mut usize) -> Result<Value, ParseError> {
    if depth > MAX_DEPTH {
        return Err(ParseError::TooDeep);
    }
    *remaining = remaining.checked_sub(1).ok_or(ParseError::TooManyNodes)?;
    Ok(match value {
        toml::Value::String(value) => Value::String(value),
        toml::Value::Integer(value) => Value::Integer(value),
        toml::Value::Float(value) => Value::Float(value),
        toml::Value::Boolean(value) => Value::Boolean(value),
        toml::Value::Datetime(value) => Value::DateTime(value.to_string()),
        toml::Value::Array(values) => Value::Array(
            values
                .into_iter()
                .map(|value| convert(value, depth + 1, remaining))
                .collect::<Result<_, _>>()?,
        ),
        toml::Value::Table(values) => Value::Table(
            values
                .into_iter()
                .map(|(key, value)| Ok((key, convert(value, depth + 1, remaining)?)))
                .collect::<Result<_, ParseError>>()?,
        ),
    })
}
