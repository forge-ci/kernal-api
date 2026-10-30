// aux-build:kernal_api.rs
// edition:2021

// A client calling blocking kernal-api operations directly in async bodies,
// beside the same calls in synchronous code and inside the closure handed to
// `launch_blocking`, which the lint must leave alone.
#![allow(unused)]

extern crate kernal_api;

use kernal_api::async_engine::{launch_blocking, yield_now, Mutex, Runtime};
use kernal_api::platform::fs::lock_exclusive_for;
use std::time::Duration;

async fn blocks(runtime: &Runtime, mutex: &Mutex<u8>) {
    let budget = Duration::from_secs(1);
    mutex.blocking_lock_within(budget);
    lock_exclusive_for(budget);
    runtime.run_within(budget);
    yield_now().await;
}

fn blocks_in_async_block(mutex: &Mutex<u8>) -> impl std::future::Future<Output = ()> + '_ {
    async move {
        Mutex::blocking_lock_within(mutex, Duration::from_secs(1));
    }
}

async fn offloaded(mutex: &'static Mutex<u8>) {
    launch_blocking(move || {
        mutex.blocking_lock_within(Duration::from_secs(1));
        lock_exclusive_for(Duration::from_secs(1));
    });
    yield_now().await;
}

fn synchronous(runtime: &Runtime, mutex: &Mutex<u8>) {
    let budget = Duration::from_secs(1);
    mutex.blocking_lock_within(budget);
    lock_exclusive_for(budget);
    runtime.run_within(budget);
}

fn main() {}
