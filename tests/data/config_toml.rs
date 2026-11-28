#![cfg(feature = "config-toml")]

use kernal_api::config::{Document, ParseError, Value, MAX_INPUT_BYTES, MAX_NODES};

#[test]
fn preserves_toml_values_without_exposing_parser_types() {
    let document = Document::parse_toml("title = '日本語'\nn = 42\nok = true\nf = 1.5\nwhen = 1979-05-27\n[flags]\nitems = ['-O0', '-pthread']\n").unwrap();
    let Value::Table(root) = document.root() else {
        panic!("root table")
    };
    assert_eq!(root["title"], Value::String("日本語".into()));
    assert_eq!(root["n"], Value::Integer(42));
    assert_eq!(root["ok"], Value::Boolean(true));
    assert_eq!(root["f"], Value::Float(1.5));
    assert_eq!(root["when"], Value::DateTime("1979-05-27".into()));
    let Value::Table(flags) = &root["flags"] else {
        panic!("flags table")
    };
    assert_eq!(
        flags["items"],
        Value::Array(vec![
            Value::String("-O0".into()),
            Value::String("-pthread".into())
        ])
    );
}

#[test]
fn rejects_invalid_documents_and_honors_input_bound() {
    for source in ["x =", "x=1\nx=2", "x='unterminated"] {
        assert!(matches!(
            Document::parse_toml(source),
            Err(ParseError::InvalidSyntax)
        ));
    }
    let source = format!("#{}", " ".repeat(MAX_INPUT_BYTES - 1));
    assert!(Document::parse_toml(&source).is_ok());
    assert!(matches!(
        Document::parse_toml(&(source + " ")),
        Err(ParseError::InputTooLarge)
    ));
}

#[test]
fn decoded_node_and_depth_limits_are_exact() {
    let source = format!("x=[{}]", vec!["0"; MAX_NODES - 2].join(","));
    assert!(Document::parse_toml(&source).is_ok());
    let source = format!("x=[{}]", vec!["0"; MAX_NODES - 1].join(","));
    assert!(matches!(
        Document::parse_toml(&source),
        Err(ParseError::TooManyNodes)
    ));
    let source = format!("x={}0{}", "[".repeat(31), "]".repeat(31));
    assert!(Document::parse_toml(&source).is_ok());
    let source = format!("x={}0{}", "[".repeat(32), "]".repeat(32));
    assert!(matches!(
        Document::parse_toml(&source),
        Err(ParseError::TooDeep)
    ));
}

#[test]
fn quotes_strings_that_parse_back_exactly() {
    use kernal_api::config::quote_toml_string;
    for text in [
        "plain",
        "",
        "it's",
        "a\"b\\c",
        "line\nbreak\ttab",
        "日本語",
        "'''",
    ] {
        let source = format!("k = {}", quote_toml_string(text));
        let Value::Table(root) = Document::parse_toml(&source).unwrap().root().clone() else {
            panic!("root table")
        };
        assert_eq!(root["k"], Value::String(text.into()), "{source}");
    }
}

#[test]
fn serializes_values_and_documents_round_trip() {
    use kernal_api::config::SerializeError;
    let source = "title = 'x \"y\"'\nn = -7\nok = false\nf = 2.5\nwhen = 1979-05-27T07:32:00Z\nlist = [1, 'two', [3]]\n[server]\nargs = ['--port', '80']\n[server.inner]\nk = 'v'\n";
    let document = Document::parse_toml(source).unwrap();
    let encoded = document.to_toml_string().unwrap();
    assert_eq!(Document::parse_toml(&encoded).unwrap(), document);

    let inline = document.root().to_toml_value().unwrap();
    let reparsed = Document::parse_toml(&format!("v = {inline}")).unwrap();
    let Value::Table(root) = reparsed.root() else {
        panic!("root table")
    };
    assert_eq!(&root["v"], document.root());

    assert_eq!(Value::Integer(3).to_toml_value().unwrap(), "3");
    assert_eq!(Value::Boolean(true).to_toml_value().unwrap(), "true");
    assert_eq!(
        Value::Integer(3).to_toml_document(),
        Err(SerializeError::RootNotTable)
    );
    assert_eq!(
        Value::DateTime("not a date".into()).to_toml_value(),
        Err(SerializeError::InvalidDateTime)
    );
    let mut deep = Value::Integer(0);
    for _ in 0..40 {
        deep = Value::Array(vec![deep]);
    }
    assert_eq!(deep.to_toml_value(), Err(SerializeError::TooDeep));
}
