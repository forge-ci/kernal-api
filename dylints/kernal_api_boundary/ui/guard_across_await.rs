// aux-build:kernal_api.rs
// edition:2021

// A client holding a kernal-api data guard across an `.await`, alone and
// hidden in an `Option`, a tuple and a local struct, beside the release
// patterns the lint must leave alone.
#![allow(unused)]

extern crate kernal_api;

use kernal_api::async_engine::{yield_now, Mutex, MutexGuard};

struct Holder<'a> {
    guard: MutexGuard<'a, u8>,
}

async fn held(mutex: &Mutex<u8>) {
    let guard = mutex.try_lock();
    yield_now().await;
    drop(guard);
}

async fn held_in_aggregates(mutex: &Mutex<u8>) {
    let pair = (1_u8, mutex.try_lock().unwrap());
    let holder = Holder {
        guard: mutex.try_lock().unwrap(),
    };
    yield_now().await;
    drop((pair, holder));
}

fn held_in_async_block(mutex: &Mutex<u8>) -> impl std::future::Future<Output = ()> + '_ {
    async move {
        let guard = mutex.try_lock().unwrap();
        yield_now().await;
        drop(guard);
    }
}

async fn released(mutex: &Mutex<u8>) {
    {
        let guard = mutex.try_lock();
    }
    let guard = mutex.try_lock();
    drop(guard);
    yield_now().await;
}

fn main() {}
