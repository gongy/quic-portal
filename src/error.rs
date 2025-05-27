use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use thiserror::Error;

#[derive(Error, Debug)]
pub enum PortalError {
    #[error("QUIC connection error: {0}")]
    QuicError(String),

    #[error("WebSocket error: {0}")]
    WebSocketError(String),

    #[error("NAT traversal error: {0}")]
    NatError(String),

    #[error("Runtime error: {0}")]
    RuntimeError(String),

    #[error("Not connected to QUIC server")]
    NotConnected,

    #[error("IO error: {0}")]
    IoError(#[from] std::io::Error),

    #[error("QUIC protocol error: {0}")]
    QuinnError(#[from] quinn::ConnectionError),

    #[error("QUIC connect error: {0}")]
    ConnectError(#[from] quinn::ConnectError),
}

impl From<PortalError> for PyErr {
    fn from(err: PortalError) -> PyErr {
        PyRuntimeError::new_err(err.to_string())
    }
}

pub type PortalResult<T> = Result<T, PortalError>;
