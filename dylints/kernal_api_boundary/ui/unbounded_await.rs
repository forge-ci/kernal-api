// aux-build:kernal_api.rs
// edition:2021

// A client awaiting a kernal-api `Task` directly, by value and by reference,
// beside the bounded join and an unrelated future it must leave alone.
#![allow(unused)]

extern crate kernal_api;

use kernal_api::async_engine::{yield_now, Task};
use std::time::Duration;

async fn unbounded(task: Task<u8>, mut other: Task<u8>) {
    let value = task.await;
    let other = (&mut other).await;
}

async fn bounded(task: Task<u8>) {
    let value = task.join_within(Duration::from_secs(1)).await;
    yield_now().await;
}

fn main() {}
