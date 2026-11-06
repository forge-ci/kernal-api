fn main() {
    // Keep the host entry point exported by this executable without requiring
    // the generated bindings cdylib (or any other dependency) to define it.
    if std::env::var("TARGET").as_deref() == Ok("wasm32-wasip1-threads") {
        println!("cargo::rustc-link-arg-bin=kernal-api-threaded-smoke=--export=kernal-api-run");
    }
}
