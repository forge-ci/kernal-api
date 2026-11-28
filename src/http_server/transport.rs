use std::{
    future::Future,
    io,
    pin::Pin,
    sync::{Arc, Mutex, Weak},
    task::{Context, Poll},
    time::Duration,
};
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};

/// Keep Hyper's HTTP/2 stream tasks scoped to the owning connection.
#[derive(Clone)]
pub(super) struct ConnectionExecutor(Weak<Mutex<tokio::task::JoinSet<()>>>);

impl ConnectionExecutor {
    pub(super) fn new(tasks: &Arc<Mutex<tokio::task::JoinSet<()>>>) -> Self {
        Self(Arc::downgrade(tasks))
    }
}

impl<F: Future<Output = ()> + Send + 'static> hyper::rt::Executor<F> for ConnectionExecutor {
    fn execute(&self, future: F) {
        if let Some(tasks) = self.0.upgrade() {
            let mut tasks = tasks.lock().expect("HTTP/2 task set mutex poisoned");
            while tasks.try_join_next().is_some() {}
            tasks.spawn(future);
        }
    }
}

/// A write-progress budget, armed only while an attempted write/flush/shutdown
/// is pending. Idle application streams do not spend this budget.
pub(super) struct ProgressIo<S> {
    socket: S,
    timeout: Duration,
    deadline: Option<Pin<Box<tokio::time::Sleep>>>,
    detection: Option<(Pin<Box<tokio::time::Sleep>>, &'static [u8])>,
}

impl<S> ProgressIo<S> {
    pub(super) fn new(socket: S, timeout: Duration) -> Self {
        Self {
            socket,
            timeout,
            deadline: None,
            detection: None,
        }
    }

    pub(super) fn with_detection_deadline(mut self, timeout: Duration) -> Self {
        self.detection = Some((
            Box::pin(tokio::time::sleep(timeout)),
            b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n",
        ));
        self
    }

    fn expired(&mut self, cx: &mut Context<'_>) -> bool {
        self.deadline
            .as_mut()
            .is_some_and(|timer| timer.as_mut().poll(cx).is_ready())
    }

    fn wait<T>(&mut self, cx: &mut Context<'_>) -> Poll<io::Result<T>> {
        self.deadline
            .get_or_insert_with(|| Box::pin(tokio::time::sleep(self.timeout)));
        if self.expired(cx) {
            Poll::Ready(Err(stalled()))
        } else {
            Poll::Pending
        }
    }
}

fn stalled() -> io::Error {
    io::Error::new(
        io::ErrorKind::TimedOut,
        "HTTP response write made no progress",
    )
}

impl<S: AsyncRead + Unpin> AsyncRead for ProgressIo<S> {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        let Some((deadline, preface)) = this.detection.as_mut() else {
            return Pin::new(&mut this.socket).poll_read(cx, buffer);
        };
        if deadline.as_mut().poll(cx).is_ready() {
            return Poll::Ready(Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "HTTP protocol detection deadline elapsed",
            )));
        }
        let start = buffer.filled().len();
        std::task::ready!(Pin::new(&mut this.socket).poll_read(cx, buffer))?;
        let received = &buffer.filled()[start..];
        // Auto detection completes on a mismatch or a full HTTP/2 preface.
        if preface.starts_with(received) && received.len() < preface.len() {
            *preface = &preface[received.len()..];
        } else {
            this.detection = None;
        }
        Poll::Ready(Ok(()))
    }
}

impl<S: AsyncWrite + Unpin> AsyncWrite for ProgressIo<S> {
    fn poll_write(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        bytes: &[u8],
    ) -> Poll<io::Result<usize>> {
        let this = self.get_mut();
        if this.expired(cx) {
            return Poll::Ready(Err(stalled()));
        }
        match Pin::new(&mut this.socket).poll_write(cx, bytes) {
            Poll::Pending => this.wait(cx),
            Poll::Ready(result) => {
                if matches!(result, Ok(n) if n > 0) {
                    this.deadline = None;
                }
                Poll::Ready(result)
            }
        }
    }

    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        if this.expired(cx) {
            return Poll::Ready(Err(stalled()));
        }
        match Pin::new(&mut this.socket).poll_flush(cx) {
            Poll::Pending => this.wait(cx),
            Poll::Ready(result) => {
                if result.is_ok() {
                    this.deadline = None;
                }
                Poll::Ready(result)
            }
        }
    }

    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        if this.expired(cx) {
            return Poll::Ready(Err(stalled()));
        }
        match Pin::new(&mut this.socket).poll_shutdown(cx) {
            Poll::Pending => this.wait(cx),
            Poll::Ready(result) => {
                if result.is_ok() {
                    this.deadline = None;
                }
                Poll::Ready(result)
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::ProgressIo;
    use std::{io, time::Duration};
    use tokio::io::AsyncWriteExt;

    #[tokio::test(start_paused = true)]
    async fn write_timeout_starts_on_attempt_not_idle_connection_creation() {
        let (socket, _reader) = tokio::io::duplex(1);
        let mut writer = ProgressIo::new(socket, Duration::from_secs(10));
        tokio::time::sleep(Duration::from_secs(100)).await;
        writer.write_all(b"a").await.unwrap();
        let start = tokio::time::Instant::now();
        let error = writer.write_all(b"b").await.unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::TimedOut);
        assert_eq!(start.elapsed(), Duration::from_secs(10));
    }
}
