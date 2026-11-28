use super::{increment, Diagnostics, Limits};
use http_body_util::{BodyExt, Limited};
use hyper::body::Incoming;
use std::io;
use tokio::time::{timeout_at, Instant};

#[derive(Debug)]
pub(super) struct IncomingBody {
    body: Limited<Incoming>,
    deadline: Instant,
    diagnostics: Diagnostics,
    failure: Option<io::ErrorKind>,
    done: bool,
}

impl IncomingBody {
    pub(super) fn new(body: Incoming, limits: Limits, diagnostics: Diagnostics) -> Self {
        Self {
            body: Limited::new(body, limits.max_request_body_bytes),
            deadline: Instant::now() + limits.body_timeout,
            diagnostics,
            failure: None,
            done: false,
        }
    }

    pub(super) async fn next_chunk(&mut self) -> io::Result<Option<Vec<u8>>> {
        if let Some(kind) = self.failure {
            return Err(kind.into());
        }
        if self.done {
            return Ok(None);
        }
        loop {
            match timeout_at(self.deadline, self.body.frame()).await {
                Ok(Some(Ok(frame))) => {
                    if let Ok(data) = frame.into_data() {
                        return Ok(Some(data.to_vec()));
                    }
                }
                Ok(None) => {
                    self.done = true;
                    return Ok(None);
                }
                Ok(Some(Err(error))) => {
                    increment(&self.diagnostics.0.request_rejections);
                    let kind = if error.is::<http_body_util::LengthLimitError>() {
                        io::ErrorKind::FileTooLarge
                    } else {
                        io::ErrorKind::InvalidData
                    };
                    self.failure = Some(kind);
                    return Err(io::Error::new(kind, error));
                }
                Err(error) => {
                    increment(&self.diagnostics.0.body_timeouts);
                    self.failure = Some(io::ErrorKind::TimedOut);
                    return Err(io::Error::new(io::ErrorKind::TimedOut, error));
                }
            }
        }
    }
}
