// edition:2021

// A stand-in for the facade's wait surface, with the real module, type and
// function names the unbounded-wait lint resolves.
#![allow(unused)]

use std::time::Duration;

pub struct PlatformChild;

impl PlatformChild {
    pub fn wait(&mut self) {}
    pub fn wait_within(&mut self, _timeout: Duration) {}
}

pub mod async_engine {
    use std::time::Duration;

    pub use self::mutex::{Mutex, MutexGuard};

    pub async fn yield_now() {}

    pub struct Runtime;

    impl Runtime {
        pub fn run(&self) {}
        pub fn run_within(&self, _timeout: Duration) {}
    }

    pub struct Semaphore;

    impl Semaphore {
        pub fn acquire(&self) {}
        pub fn acquire_within(&self, _timeout: Duration) {}
        pub fn available_permits(&self) -> usize {
            0
        }
    }

    pub mod mutex {
        use std::time::Duration;

        pub struct Mutex<T>(pub T);

        pub struct MutexGuard<'a, T>(pub &'a T);

        impl<T> Drop for MutexGuard<'_, T> {
            fn drop(&mut self) {}
        }

        impl<T> Mutex<T> {
            pub fn new(value: T) -> Self {
                Mutex(value)
            }
            pub fn try_lock(&self) -> Option<MutexGuard<'_, T>> {
                Some(MutexGuard(&self.0))
            }
            pub fn lock(&self) {}
            pub fn lock_within(&self, _timeout: Duration) {}
        }
    }
}

pub mod crash {
    // Same type and method name as the async engine's, in another module:
    // not a wait.
    pub struct Runtime;

    impl Runtime {
        pub fn run(&self) {}
    }
}

pub mod platform {
    pub mod fs {
        use std::time::Duration;

        pub fn lock_exclusive() {}
        pub fn lock_exclusive_for(_timeout: Duration) {}
    }
}
