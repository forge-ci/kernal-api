// aux-build:kernal_api.rs

// A client calling an unbounded kernal-api wait, through a method call, a
// UFCS path, a function item and an alias import, beside the bounded forms
// and same-named non-waits it must leave alone.
#![allow(unused)]

extern crate kernal_api;

use kernal_api::async_engine::{Mutex, Runtime, Semaphore};
use kernal_api::platform::fs::lock_exclusive as take_lock;
use std::time::Duration;

fn unbounded(runtime: &Runtime, semaphore: &Semaphore, mutex: &Mutex<u8>) {
    runtime.run();
    Semaphore::acquire(semaphore);
    let lock = Mutex::<u8>::lock;
    lock(mutex);
    take_lock();
    kernal_api::PlatformChild.wait();
}

fn bounded(runtime: &Runtime, semaphore: &Semaphore, mutex: &Mutex<u8>) {
    let budget = Duration::from_secs(1);
    runtime.run_within(budget);
    semaphore.acquire_within(budget);
    semaphore.available_permits();
    mutex.lock_within(budget);
    let _ = Mutex::new(0_u8);
    kernal_api::platform::fs::lock_exclusive_for(budget);
    kernal_api::PlatformChild.wait_within(budget);
    kernal_api::crash::Runtime.run();
}

fn main() {}
