use pyo3::prelude::*;
use pyo3::types::PyBytes;
use std::sync::Arc;
use tokio::runtime::Runtime;
use tokio::sync::Mutex;

mod error;
mod quic;

use error::{PortalError, PortalResult};
use quic::{QuicClient, QuicServer, TransportOptions};

/// Python wrapper for TransportOptions
#[pyclass]
#[derive(Clone)]
pub struct QuicTransportOptions {
    inner: TransportOptions,
}

#[pymethods]
impl QuicTransportOptions {
    #[new]
    fn new() -> Self {
        Self {
            inner: TransportOptions::new(),
        }
    }

    #[getter]
    fn max_idle_timeout_secs(&self) -> u64 {
        self.inner.max_idle_timeout_secs
    }

    #[setter]
    fn set_max_idle_timeout_secs(&mut self, value: u64) {
        self.inner.max_idle_timeout_secs = value;
    }

    #[getter]
    fn congestion_controller_type(&self) -> String {
        self.inner.congestion_controller_type.clone()
    }

    #[setter]
    fn set_congestion_controller_type(&mut self, value: String) {
        self.inner.congestion_controller_type = value;
    }

    #[getter]
    fn initial_window(&self) -> u64 {
        self.inner.initial_window
    }

    #[setter]
    fn set_initial_window(&mut self, value: u64) {
        self.inner.initial_window = value;
    }

    #[getter]
    fn keep_alive_interval_secs(&self) -> u64 {
        self.inner.keep_alive_interval_secs
    }

    #[setter]
    fn set_keep_alive_interval_secs(&mut self, value: u64) {
        self.inner.keep_alive_interval_secs = value;
    }
}

/// Enum to hold either client or server connection
enum QuicConnection {
    Client(QuicClient),
    Server(QuicServer),
}

impl QuicConnection {
    async fn send_message(&self, data: Vec<u8>) -> PortalResult<()> {
        match self {
            QuicConnection::Client(client) => client.send_message(data).await,
            QuicConnection::Server(server) => server.send_message(data).await,
        }
    }

    async fn recv_message(
        &self,
        timeout: Option<std::time::Duration>,
    ) -> PortalResult<Option<Vec<u8>>> {
        match self {
            QuicConnection::Client(client) => client.recv_message(timeout).await,
            QuicConnection::Server(server) => server.recv_message(timeout).await,
        }
    }

    async fn close(self) -> PortalResult<()> {
        match self {
            QuicConnection::Client(client) => client.close().await,
            QuicConnection::Server(server) => server.close().await,
        }
    }
}

/// A high-performance QUIC portal for Modal applications
#[pyclass]
pub struct QuicPortal {
    runtime: Arc<Runtime>,
    connection: Arc<Mutex<Option<QuicConnection>>>,
}

#[pymethods]
impl QuicPortal {
    /// Create a new QUIC portal
    #[new]
    fn new() -> PortalResult<Self> {
        let runtime = Arc::new(
            tokio::runtime::Builder::new_multi_thread()
                .enable_all()
                .build()
                .map_err(|e| PortalError::RuntimeError(e.to_string()))?,
        );

        Ok(QuicPortal {
            runtime,
            connection: Arc::new(Mutex::new(None)),
        })
    }

    /// Connect to QUIC server after NAT traversal is complete (client mode)
    fn connect(
        &self,
        py: Python,
        server_ip: String,
        server_port: u16,
        local_port: u16,
        transport_options: QuicTransportOptions,
    ) -> PortalResult<()> {
        let runtime = self.runtime.clone();
        let connection = self.connection.clone();

        py.allow_threads(move || {
            runtime.block_on(async move {
                let client = QuicClient::connect(&server_ip, server_port, local_port, &transport_options.inner).await?;
                *connection.lock().await = Some(QuicConnection::Client(client));
                Ok(())
            })
        })
    }

    /// Start QUIC server and wait for connection (server mode)
    fn listen(&self, py: Python, local_port: u16, transport_options: QuicTransportOptions) -> PortalResult<()> {
        let runtime = self.runtime.clone();
        let connection = self.connection.clone();

        py.allow_threads(move || {
            runtime.block_on(async move {
                let server = QuicServer::listen_and_accept(local_port, &transport_options.inner).await?;
                *connection.lock().await = Some(QuicConnection::Server(server));
                Ok(())
            })
        })
    }

    /// Send bytes over QUIC (WebSocket-style: length-prefixed, no response expected)
    fn send(&self, py: Python, data: Bound<'_, PyBytes>) -> PortalResult<()> {
        let data = data.as_bytes().to_vec();
        let runtime = self.runtime.clone();
        let connection = self.connection.clone();

        py.allow_threads(move || {
            runtime.block_on(async move {
                let conn_guard = connection.lock().await;
                if let Some(conn) = conn_guard.as_ref() {
                    conn.send_message(data).await?;
                    Ok(())
                } else {
                    Err(PortalError::NotConnected)
                }
            })
        })
    }

    /// Receive data from QUIC connection (WebSocket-style: blocks until message arrives)
    fn recv(&self, py: Python, timeout_ms: Option<u64>) -> PortalResult<Option<PyObject>> {
        let runtime = self.runtime.clone();
        let connection = self.connection.clone();

        py.allow_threads(move || {
            runtime.block_on(async move {
                let conn_guard = connection.lock().await;
                if let Some(conn) = conn_guard.as_ref() {
                    let timeout = timeout_ms.map(|ms| std::time::Duration::from_millis(ms));
                    match conn.recv_message(timeout).await? {
                        Some(data) => {
                            Python::with_gil(|py| Ok(Some(PyBytes::new(py, &data).into())))
                        }
                        None => Ok(None),
                    }
                } else {
                    Err(PortalError::NotConnected)
                }
            })
        })
    }

    /// Check if connected to QUIC server
    fn is_connected(&self, py: Python) -> bool {
        let connection = self.connection.clone();
        let runtime = self.runtime.clone();

        py.allow_threads(move || runtime.block_on(async move { connection.lock().await.is_some() }))
    }

    /// Close all connections
    fn close(&self, py: Python) -> PortalResult<()> {
        let runtime = self.runtime.clone();
        let connection = self.connection.clone();

        py.allow_threads(move || {
            runtime.block_on(async move {
                if let Some(conn) = connection.lock().await.take() {
                    conn.close().await?;
                }
                Ok(())
            })
        })
    }
}

/// Python module definition
#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    pyo3_log::init();

    m.add_class::<QuicPortal>()?;
    m.add_class::<QuicTransportOptions>()?;

    // Add version
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;

    Ok(())
}
