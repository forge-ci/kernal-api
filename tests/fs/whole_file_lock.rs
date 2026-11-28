//! Whole-file advisory locks exclude one another through the public facade
//! on every host, and release on drop or explicit unlock.
#![cfg(feature = "fs")]

use std::io;
use std::time::Duration;

use kernal_api::platform::fs::{
    is_lock_conflict, lock_exclusive_whole_for, lock_exclusive_whole_owned_for,
    lock_shared_whole_for, lock_shared_whole_owned_for, open_lock_file, try_lock_exclusive_whole,
    try_lock_exclusive_whole_owned, try_lock_shared_whole, try_lock_shared_whole_owned,
};

const WAIT: Duration = Duration::from_secs(10);

fn assert_conflict<T>(result: io::Result<T>) {
    match result {
        Err(error) => assert!(is_lock_conflict(&error), "{error}"),
        Ok(_) => panic!("lock acquired while another handle held it"),
    }
}

fn assert_timed_out<T>(result: io::Result<T>) {
    match result {
        Err(error) => assert_eq!(error.kind(), io::ErrorKind::TimedOut, "{error}"),
        Ok(_) => panic!("lock acquired while another handle held it"),
    }
}

#[test]
fn exclusive_whole_lock_excludes_every_whole_locker_until_dropped() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("lock");
    let holder = open_lock_file(&path).unwrap();
    let held = try_lock_exclusive_whole(&holder).unwrap();

    let contender = open_lock_file(&path).unwrap();
    assert_conflict(try_lock_exclusive_whole(&contender));
    assert_conflict(try_lock_shared_whole(&contender));
    assert_conflict(try_lock_exclusive_whole_owned(
        open_lock_file(&path).unwrap(),
    ));
    assert_conflict(try_lock_shared_whole_owned(open_lock_file(&path).unwrap()));
    let budget = Duration::from_millis(50);
    assert_timed_out(lock_exclusive_whole_for(&contender, budget));
    assert_timed_out(lock_shared_whole_for(&contender, budget));
    assert_timed_out(lock_exclusive_whole_owned_for(
        open_lock_file(&path).unwrap(),
        budget,
    ));
    assert_timed_out(lock_shared_whole_owned_for(
        open_lock_file(&path).unwrap(),
        budget,
    ));

    drop(held);
    drop(lock_exclusive_whole_for(&contender, WAIT).unwrap());
}

#[test]
fn shared_whole_locks_coexist_and_block_an_exclusive_one() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("lock");
    let first = open_lock_file(&path).unwrap();
    let second = open_lock_file(&path).unwrap();
    let a = lock_shared_whole_for(&first, WAIT).unwrap();
    let b = try_lock_shared_whole(&second).unwrap();

    let writer = open_lock_file(&path).unwrap();
    assert_conflict(try_lock_exclusive_whole(&writer));
    drop(a);
    drop(b);
    drop(try_lock_exclusive_whole(&writer).unwrap());
}

#[test]
fn owned_whole_lock_unlocks_with_its_own_range() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("lock");
    let held = lock_exclusive_whole_owned_for(open_lock_file(&path).unwrap(), WAIT).unwrap();

    let contender = open_lock_file(&path).unwrap();
    assert_conflict(try_lock_shared_whole(&contender));

    // An explicit unlock must release the whole-file range, not the default
    // sentinel byte, or the lock would stay held on Windows.
    let file = held.unlock().map_err(|(_, error)| error).unwrap();
    drop(try_lock_exclusive_whole(&contender).unwrap());
    drop(file);
}
