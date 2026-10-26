//! Facade-owned asynchronous exclusive locking.
//!
//! The lock is fair: waiters acquire in the order their acquisitions first
//! queued, and it does not poison when a holder panics. Every guard is a
//! facade-owned newtype; no backend guard type crosses the facade.

use std::ops::{Deref, DerefMut};
use std::sync::Arc;

/// An asynchronous mutual-exclusion lock with FIFO waiter order.
///
/// Borrowed guards ([`Self::lock`], [`Self::try_lock`],
/// [`Self::blocking_lock`]) tie the lease to a borrow of the lock. Owned
/// guards ([`Self::lock_owned`], [`Self::try_lock_owned`]) take an
/// `Arc<Mutex<T>>` and may be moved into launched work or stored alongside
/// the value they protect.
///
/// An owned guard retains the very `Arc<Mutex<T>>` it was acquired through,
/// not merely the protected storage. A registry that keys locks by
/// `Weak<Mutex<T>>` therefore keeps observing a held lock after every other
/// strong handle has been dropped: `Weak::upgrade` succeeds for as long as
/// any owned guard is alive, so a second caller receives the same lock and
/// waits for it instead of minting an unrelated one.
pub struct Mutex<T> {
    inner: Arc<tokio::sync::Mutex<T>>,
}

impl<T> Mutex<T> {
    /// Create an unlocked value.
    pub fn new(value: T) -> Self {
        Self {
            inner: Arc::new(tokio::sync::Mutex::new(value)),
        }
    }

    /// Acquire a borrowed guard, queuing behind earlier waiters.
    ///
    /// Cancelling the pending acquisition loses its queue position without
    /// acquiring the lock.
    pub async fn lock(&self) -> MutexGuard<'_, T> {
        MutexGuard {
            inner: self.inner.lock().await,
        }
    }

    /// Acquire an owned guard that keeps this `Arc<Mutex<T>>` alive until the
    /// guard is dropped.
    ///
    /// Cancelling the pending acquisition loses its queue position without
    /// acquiring the lock.
    pub async fn lock_owned(self: Arc<Self>) -> OwnedMutexGuard<T> {
        let inner = Arc::clone(&self.inner).lock_owned().await;
        OwnedMutexGuard {
            inner,
            _owner: self,
        }
    }

    /// Acquire a borrowed guard, waiting at most `timeout`. An expired wait
    /// leaves the queue without acquiring the lock.
    ///
    /// # Errors
    ///
    /// Returns [`super::LockTimedOut`] if the lock was not acquired in time.
    /// Requires a runtime with its timer enabled.
    pub async fn lock_within(
        &self,
        timeout: std::time::Duration,
    ) -> Result<MutexGuard<'_, T>, super::LockTimedOut> {
        super::timeout(timeout, self.lock())
            .await
            .map_err(|_| super::LockTimedOut)
    }

    /// Acquire an owned guard, waiting at most `timeout`.
    ///
    /// # Errors
    ///
    /// As [`Self::lock_within`].
    pub async fn lock_owned_within(
        self: Arc<Self>,
        timeout: std::time::Duration,
    ) -> Result<OwnedMutexGuard<T>, super::LockTimedOut> {
        super::timeout(timeout, self.lock_owned())
            .await
            .map_err(|_| super::LockTimedOut)
    }

    /// Acquire a borrowed guard without waiting.
    ///
    /// This never bypasses an already queued waiter.
    ///
    /// # Errors
    ///
    /// Returns [`MutexTryLockError`] when the lock is held or contended.
    pub fn try_lock(&self) -> Result<MutexGuard<'_, T>, MutexTryLockError> {
        self.inner
            .try_lock()
            .map(|inner| MutexGuard { inner })
            .map_err(|_| MutexTryLockError)
    }

    /// Acquire an owned guard without waiting.
    ///
    /// This never bypasses an already queued waiter. On success the guard
    /// keeps this `Arc<Mutex<T>>` alive, exactly as [`Self::lock_owned`] does.
    ///
    /// # Errors
    ///
    /// Returns [`MutexTryLockError`] when the lock is held or contended.
    pub fn try_lock_owned(self: Arc<Self>) -> Result<OwnedMutexGuard<T>, MutexTryLockError> {
        let inner = Arc::clone(&self.inner)
            .try_lock_owned()
            .map_err(|_| MutexTryLockError)?;
        Ok(OwnedMutexGuard {
            inner,
            _owner: self,
        })
    }

    /// Block this thread until a borrowed guard is available.
    ///
    /// # Panics
    ///
    /// Panics when called from inside an asynchronous execution context. Use
    /// it only from a synchronous thread or the runtime's blocking-work lane
    /// ([`super::launch_blocking`]).
    pub fn blocking_lock(&self) -> MutexGuard<'_, T> {
        MutexGuard {
            inner: self.inner.blocking_lock(),
        }
    }

    /// Block this thread until a borrowed guard is available or `timeout`
    /// elapses.
    ///
    /// # Errors
    ///
    /// Returns [`super::LockTimedOut`] if the guard is not available within
    /// `timeout`; no guard or queue position is kept.
    ///
    /// # Panics
    ///
    /// Panics when called from inside an asynchronous execution context, like
    /// the unbounded form.
    pub fn blocking_lock_within(
        &self,
        timeout: std::time::Duration,
    ) -> Result<MutexGuard<'_, T>, super::LockTimedOut> {
        assert!(
            super::RuntimeHandle::current().is_err(),
            "a bounded blocking lock must not park an asynchronous execution context"
        );
        super::park_within(self.inner.lock(), timeout)
            .map(|inner| MutexGuard { inner })
            .ok_or(super::LockTimedOut)
    }
}

impl<T: Default> Default for Mutex<T> {
    fn default() -> Self {
        Self::new(T::default())
    }
}

impl<T> std::fmt::Debug for Mutex<T> {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.debug_struct("Mutex").finish_non_exhaustive()
    }
}

/// Exclusive lease returned by [`Mutex::lock`], [`Mutex::try_lock`], and
/// [`Mutex::blocking_lock`]. Dropping it releases the lock.
pub struct MutexGuard<'a, T> {
    inner: tokio::sync::MutexGuard<'a, T>,
}

impl<T> Deref for MutexGuard<'_, T> {
    type Target = T;

    fn deref(&self) -> &T {
        &self.inner
    }
}

impl<T> DerefMut for MutexGuard<'_, T> {
    fn deref_mut(&mut self) -> &mut T {
        &mut self.inner
    }
}

/// Owned exclusive lease returned by [`Mutex::lock_owned`] and
/// [`Mutex::try_lock_owned`]. Dropping it releases the lock.
///
/// The guard holds a strong reference to the `Arc<Mutex<T>>` it was acquired
/// through, so that facade handle stays alive -- and every `Weak` to it stays
/// upgradable -- while the lock is held.
pub struct OwnedMutexGuard<T> {
    // Declared first so it drops first: the lock is released before the
    // facade handle it was acquired through.
    inner: tokio::sync::OwnedMutexGuard<T>,
    _owner: Arc<Mutex<T>>,
}

impl<T> Deref for OwnedMutexGuard<T> {
    type Target = T;

    fn deref(&self) -> &T {
        &self.inner
    }
}

impl<T> DerefMut for OwnedMutexGuard<T> {
    fn deref_mut(&mut self) -> &mut T {
        &mut self.inner
    }
}

impl<T: std::fmt::Debug> std::fmt::Debug for MutexGuard<'_, T> {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        std::fmt::Debug::fmt(&**self, formatter)
    }
}

impl<T: std::fmt::Debug> std::fmt::Debug for OwnedMutexGuard<T> {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        std::fmt::Debug::fmt(&**self, formatter)
    }
}

/// A non-waiting lock attempt found the mutex held or contended.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct MutexTryLockError;

impl std::fmt::Display for MutexTryLockError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("mutex is busy")
    }
}

impl std::error::Error for MutexTryLockError {}
