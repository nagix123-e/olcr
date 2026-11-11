use std::sync::Mutex;

// Lives only in the desktop process. macOS, rather than a preferences flag,
// remains the authority on persistent access across launches and updates.
pub struct CredentialCache<T> {
    value: Mutex<Option<Result<T, String>>>,
}
impl<T: Clone> CredentialCache<T> {
    pub const fn new() -> Self { Self { value: Mutex::new(None) } }
    pub fn read(&self, authorize: bool, load: impl FnOnce() -> Result<T, String>) -> Result<T, String> {
        let mut cached = self.value.lock().map_err(|_| "credential_cache_unavailable")?;
        if authorize || cached.is_none() { *cached = Some(load()); }
        cached.as_ref().unwrap().clone()
    }
    pub fn update(&self, save: impl FnOnce() -> Result<T, String>) -> Result<(), String> {
        let mut cached = self.value.lock().map_err(|_| "credential_cache_unavailable")?;
        // Failed writes must not discard the last successfully restored secrets.
        let next = save()?;
        *cached = Some(Ok(next));
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, atomic::{AtomicUsize, Ordering}};
    #[test]
    fn concurrent_pages_share_one_restore_and_explicit_retry_replaces_failure() {
        let cache=Arc::new(CredentialCache::<usize>::new());
        let calls=Arc::new(AtomicUsize::new(0));
        let threads:Vec<_>=(0..8).map(|_| {let cache=cache.clone();let calls=calls.clone();std::thread::spawn(move||{
            assert!(cache.read(false,||{calls.fetch_add(1,Ordering::SeqCst);Err("authorization_required".into())}).is_err());
        })}).collect();
        for thread in threads { thread.join().unwrap(); }
        assert_eq!(calls.load(Ordering::SeqCst),1);
        assert_eq!(cache.read(true,||Ok(3)).unwrap(),3);
        assert_eq!(cache.read(false,||panic!("page navigation must use cache")).unwrap(),3);
    }
    #[test]
    fn successful_writes_refresh_cache_and_failed_writes_preserve_it() {
        let cache=CredentialCache::new();
        assert_eq!(cache.read(false,||Ok(1)).unwrap(),1);
        cache.update(||Ok(2)).unwrap();
        assert!(cache.update(||Err("denied".into())).is_err());
        assert_eq!(cache.read(false,||panic!("cached")).unwrap(),2);
    }
}
