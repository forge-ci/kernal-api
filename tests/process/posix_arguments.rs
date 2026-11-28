#![cfg(feature = "command-arguments")]

use kernal_api::arguments::{
    parse_posix, ArgumentParseError, MAX_POSIX_ARGUMENTS, MAX_POSIX_INPUT_BYTES,
};

#[test]
fn tool_arguments_preserve_quoting_without_expansion() {
    assert_eq!(
        parse_posix("-I'/path with spaces' \"\" '日本語' '$HOME' '*.cpp' $(literal)").unwrap(),
        [
            "-I/path with spaces",
            "",
            "日本語",
            "$HOME",
            "*.cpp",
            "$(literal)"
        ]
    );
}

#[test]
fn escapes_comments_and_empty_input_keep_tool_output_semantics() {
    assert_eq!(parse_posix("  # ignored\n").unwrap(), Vec::<String>::new());
    assert_eq!(
        parse_posix("a\\ b c\\\nd 'x#y' # ignored\nend").unwrap(),
        ["a b", "cd", "x#y", "end"]
    );
    assert_eq!(
        parse_posix("'open"),
        Err(ArgumentParseError::UnterminatedQuote)
    );
    assert_eq!(
        parse_posix("\"open"),
        Err(ArgumentParseError::UnterminatedQuote)
    );
    assert_eq!(parse_posix("a\0b"), Err(ArgumentParseError::ContainsNul));
}

#[test]
fn input_bytes_and_output_count_have_exact_limits() {
    let limit = "é".repeat(MAX_POSIX_INPUT_BYTES / 2);
    assert_eq!(parse_posix(&limit).unwrap(), std::slice::from_ref(&limit));
    assert_eq!(
        parse_posix(&(limit + "x")),
        Err(ArgumentParseError::InputTooLarge)
    );
    assert_eq!(
        parse_posix(&"x ".repeat(MAX_POSIX_ARGUMENTS))
            .unwrap()
            .len(),
        MAX_POSIX_ARGUMENTS
    );
    assert_eq!(
        parse_posix(&"x ".repeat(MAX_POSIX_ARGUMENTS + 1)),
        Err(ArgumentParseError::TooManyArguments)
    );
}

#[test]
fn quote_and_join_round_trip_through_parse() {
    use kernal_api::arguments::{join_posix, quote_posix};
    assert_eq!(quote_posix("plain-word"), "plain-word");
    assert_eq!(quote_posix(""), "''");
    assert_eq!(quote_posix("a b"), "'a b'");
    let words = [
        "sudo",
        "-E",
        "",
        "it's",
        "$HOME",
        "a b",
        "x\"y",
        "*",
        "日本語",
        "line\nbreak",
    ];
    let joined = join_posix(words);
    assert_eq!(parse_posix(&joined).unwrap(), words);
    for word in words {
        assert_eq!(parse_posix(&quote_posix(word)).unwrap(), [word]);
    }
    assert_eq!(join_posix(Vec::<String>::new()), "");
}
