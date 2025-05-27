use crate::error::{PortalError, PortalResult};
use quinn::{ClientConfig, Connection, Endpoint, RecvStream, SendStream, ServerConfig};
use std::net::SocketAddr;
use std::sync::Arc;
use tokio::sync::Mutex;
use tokio::time::{timeout, Duration};

/// Shared transport configuration builder for consistent QUIC settings
struct TransportConfigBuilder;

impl TransportConfigBuilder {
    fn build() -> quinn::TransportConfig {
        let mut transport_config = quinn::TransportConfig::default();

        // Increase timeouts
        transport_config
            .max_idle_timeout(Some(std::time::Duration::from_secs(60).try_into().unwrap()));

        // Optimize for large messages - increase all window sizes significantly
        transport_config.receive_window(10_000_000_u32.into()); // 10MB receive window
        transport_config.send_window(10_000_000); // 10MB send window
        transport_config.stream_receive_window(5_000_000_u32.into()); // 5MB per stream

        // Increase initial window sizes to avoid slow start for large messages
        transport_config.initial_rtt(std::time::Duration::from_millis(20)); // Assume 50ms RTT

        // Allow more concurrent streams
        transport_config.max_concurrent_bidi_streams(100_u32.into());
        transport_config.max_concurrent_uni_streams(100_u32.into());

        // Increase datagram buffer for better throughput
        transport_config.datagram_receive_buffer_size(Some(5_000_000)); // 5MB
        transport_config.datagram_send_buffer_size(5_000_000); // 5MB

        // Use custom fixed window congestion controller for truly constant 1MiB window
        transport_config.congestion_controller_factory(FixedWindowConfig::new());

        // Set minimum MTU to avoid fragmentation issues
        transport_config.min_mtu(1200); // Minimum MTU

        transport_config
    }
}

/// Configuration for fixed window congestion controller
#[derive(Debug, Clone)]
pub struct FixedWindowConfig {
    window_size: u64,
}

impl FixedWindowConfig {
    pub fn new() -> Self {
        Self {
            window_size: 1024 * 1024, // 1MiB fixed window
        }
    }
}

impl quinn::congestion::ControllerFactory for FixedWindowConfig {
    fn build(
        &self,
        _now: std::time::Instant,
        _current_mtu: u16,
    ) -> Box<dyn quinn::congestion::Controller> {
        Box::new(FixedWindowController::new(self.window_size))
    }
}

/// Custom congestion controller that maintains a truly constant window
#[derive(Debug, Clone)]
pub struct FixedWindowController {
    window_size: u64,
}

impl FixedWindowController {
    pub fn new(window_size: u64) -> Self {
        Self { window_size }
    }
}

impl quinn::congestion::Controller for FixedWindowController {
    fn window(&self) -> u64 {
        self.window_size
    }

    fn clone_box(&self) -> Box<dyn quinn::congestion::Controller> {
        Box::new(self.clone())
    }

    fn on_congestion_event(
        &mut self,
        _now: std::time::Instant,
        _sent: std::time::Instant,
        _is_persistent_congestion: bool,
        _lost_bytes: u64,
    ) {
        // Do absolutely nothing - maintain fixed window even on congestion
    }

    fn on_mtu_update(&mut self, _new_mtu: u16) {
        // Do nothing - window size independent of MTU
    }

    fn initial_window(&self) -> u64 {
        self.window_size
    }

    fn into_any(self: Box<Self>) -> Box<dyn std::any::Any> {
        self
    }

    // Override the provided methods to ensure no adaptive behavior
    fn on_sent(&mut self, _now: std::time::Instant, _bytes: u64, _last_packet_number: u64) {
        // Do nothing - no tracking of sent packets
    }

    fn on_end_acks(
        &mut self,
        _now: std::time::Instant,
        _in_flight: u64,
        _app_limited: bool,
        _largest_packet_num_acked: Option<u64>,
    ) {
        // Do nothing - no batch processing effects
    }
}

/// Shared QUIC connection functionality for both client and server
pub struct QuicConnection {
    connection: Connection,
    endpoint: Endpoint,
    // Dedicated streams for bidirectional communication
    send_stream: Arc<Mutex<Option<SendStream>>>,
    recv_stream: Arc<Mutex<Option<RecvStream>>>,
}

impl QuicConnection {
    fn new(
        connection: Connection,
        endpoint: Endpoint,
        send_stream: SendStream,
        recv_stream: RecvStream,
    ) -> Self {
        Self {
            connection,
            endpoint,
            send_stream: Arc::new(Mutex::new(Some(send_stream))),
            recv_stream: Arc::new(Mutex::new(Some(recv_stream))),
        }
    }

    /// Send a message (WebSocket-style: length-prefixed)
    pub async fn send_message(&self, data: Vec<u8>) -> PortalResult<()> {
        let mut send_guard = self.send_stream.lock().await;
        if let Some(send) = send_guard.as_mut() {
            // Send length prefix (4 bytes, big-endian)
            let length_prefix = (data.len() as u32).to_be_bytes();
            send.write_all(&length_prefix)
                .await
                .map_err(|e| PortalError::QuicError(format!("Failed to send length: {}", e)))?;

            // Send data.
            // TODO: consider chunking.
            send.write_all(&data)
                .await
                .map_err(|e| PortalError::QuicError(format!("Failed to send data: {}", e)))?;

            Ok(())
        } else {
            Err(PortalError::QuicError(
                "Send stream not available".to_string(),
            ))
        }
    }

    /// Receive a message (WebSocket-style: blocks until complete message arrives)
    pub async fn recv_message(
        &self,
        timeout_duration: Option<Duration>,
    ) -> PortalResult<Option<Vec<u8>>> {
        let mut recv_guard = self.recv_stream.lock().await;
        if let Some(recv) = recv_guard.as_mut() {
            let recv_future = async {
                // Read length prefix (4 bytes)
                let mut len_buf = [0u8; 4];
                recv.read_exact(&mut len_buf)
                    .await
                    .map_err(|e| PortalError::QuicError(format!("Failed to read length: {}", e)))?;

                let data_length = u32::from_be_bytes(len_buf) as usize;

                // Read the complete message
                let mut data = vec![0u8; data_length];
                recv.read_exact(&mut data)
                    .await
                    .map_err(|e| PortalError::QuicError(format!("Failed to read data: {}", e)))?;

                Ok(data)
            };

            if let Some(duration) = timeout_duration {
                match timeout(duration, recv_future).await {
                    Ok(result) => result.map(Some),
                    Err(_) => Ok(None), // Timeout
                }
            } else {
                recv_future.await.map(Some)
            }
        } else {
            Err(PortalError::QuicError(
                "Receive stream not available".to_string(),
            ))
        }
    }

    pub async fn close(self) -> PortalResult<()> {
        // Close streams
        if let Some(mut send) = self.send_stream.lock().await.take() {
            let _ = send.finish().await;
        }

        // Close connection
        self.connection.close(0u32.into(), b"closing");
        self.endpoint.wait_idle().await;
        Ok(())
    }
}

pub struct QuicClient {
    connection: QuicConnection,
}

pub struct QuicServer {
    connection: QuicConnection,
}

impl QuicServer {
    pub async fn listen_and_accept(local_port: u16) -> PortalResult<Self> {
        // Configure server
        let server_config = configure_server()?;
        let server_addr = format!("0.0.0.0:{}", local_port)
            .parse::<SocketAddr>()
            .map_err(|e| PortalError::QuicError(format!("Invalid server address: {}", e)))?;

        let endpoint = Endpoint::server(server_config, server_addr)?;

        // Wait for incoming connection
        let incoming = endpoint
            .accept()
            .await
            .ok_or_else(|| PortalError::QuicError("No incoming connection".to_string()))?;

        let connection = incoming
            .await
            .map_err(|e| PortalError::QuicError(format!("Failed to accept connection: {}", e)))?;

        // Accept the first bidirectional stream
        let (send_stream, recv_stream) = connection
            .accept_bi()
            .await
            .map_err(|e| PortalError::QuicError(format!("Failed to accept stream: {}", e)))?;

        Ok(QuicServer {
            connection: QuicConnection::new(connection, endpoint, send_stream, recv_stream),
        })
    }

    /// Send a message (WebSocket-style: length-prefixed)
    pub async fn send_message(&self, data: Vec<u8>) -> PortalResult<()> {
        self.connection.send_message(data).await
    }

    /// Receive a message (WebSocket-style: blocks until complete message arrives)
    pub async fn recv_message(
        &self,
        timeout_duration: Option<Duration>,
    ) -> PortalResult<Option<Vec<u8>>> {
        self.connection.recv_message(timeout_duration).await
    }

    pub async fn close(self) -> PortalResult<()> {
        self.connection.close().await
    }
}

impl QuicClient {
    pub async fn connect(server_ip: &str, server_port: u16, local_port: u16) -> PortalResult<Self> {
        // Configure client
        let client_config = configure_client()?;
        let client_addr = format!("0.0.0.0:{}", local_port)
            .parse::<SocketAddr>()
            .map_err(|e| PortalError::QuicError(format!("Invalid local address: {}", e)))?;

        let mut endpoint = Endpoint::client(client_addr)?;
        endpoint.set_default_client_config(client_config);

        let server_addr = format!("{}:{}", server_ip, server_port)
            .parse::<SocketAddr>()
            .map_err(|e| PortalError::QuicError(format!("Invalid server address: {}", e)))?;

        // Connect to server
        let connection = endpoint
            .connect(server_addr, "quic-portal")?
            .await
            .map_err(|e| PortalError::QuicError(format!("Connection failed: {}", e)))?;

        // Open a bidirectional stream for communication
        let (send_stream, recv_stream) = connection
            .open_bi()
            .await
            .map_err(|e| PortalError::QuicError(format!("Failed to open stream: {}", e)))?;

        Ok(QuicClient {
            connection: QuicConnection::new(connection, endpoint, send_stream, recv_stream),
        })
    }

    /// Send a message (WebSocket-style: length-prefixed)
    pub async fn send_message(&self, data: Vec<u8>) -> PortalResult<()> {
        self.connection.send_message(data).await
    }

    /// Receive a message (WebSocket-style: blocks until complete message arrives)
    pub async fn recv_message(
        &self,
        timeout_duration: Option<Duration>,
    ) -> PortalResult<Option<Vec<u8>>> {
        self.connection.recv_message(timeout_duration).await
    }

    pub async fn close(self) -> PortalResult<()> {
        self.connection.close().await
    }
}

fn configure_client() -> PortalResult<ClientConfig> {
    // Create a dangerous client config that skips certificate verification
    // (for development/testing only!)
    let crypto = rustls::ClientConfig::builder()
        .with_safe_defaults()
        .with_custom_certificate_verifier(Arc::new(SkipServerVerification))
        .with_no_client_auth();

    let mut client_config = ClientConfig::new(Arc::new(crypto));

    // Use shared transport configuration
    client_config.transport_config(Arc::new(TransportConfigBuilder::build()));

    Ok(client_config)
}

fn configure_server() -> PortalResult<ServerConfig> {
    // Generate a self-signed certificate for development
    let cert = rcgen::generate_simple_self_signed(vec!["localhost".into(), "quic-portal".into()])
        .map_err(|e| {
        PortalError::QuicError(format!("Failed to generate certificate: {}", e))
    })?;

    let cert_der = cert
        .serialize_der()
        .map_err(|e| PortalError::QuicError(format!("Failed to serialize certificate: {}", e)))?;
    let priv_key = cert.serialize_private_key_der();

    let priv_key = rustls::PrivateKey(priv_key);
    let cert_chain = vec![rustls::Certificate(cert_der)];

    let crypto = rustls::ServerConfig::builder()
        .with_safe_defaults()
        .with_no_client_auth()
        .with_single_cert(cert_chain, priv_key)
        .map_err(|e| PortalError::QuicError(format!("Failed to configure TLS: {}", e)))?;

    let mut server_config = ServerConfig::with_crypto(Arc::new(crypto));

    // Use shared transport configuration
    server_config.transport_config(Arc::new(TransportConfigBuilder::build()));

    Ok(server_config)
}

// Custom certificate verifier that accepts all certificates (for development only!)
#[derive(Debug)]
struct SkipServerVerification;

impl rustls::client::ServerCertVerifier for SkipServerVerification {
    fn verify_server_cert(
        &self,
        _end_entity: &rustls::Certificate,
        _intermediates: &[rustls::Certificate],
        _server_name: &rustls::ServerName,
        _scts: &mut dyn Iterator<Item = &[u8]>,
        _ocsp_response: &[u8],
        _now: std::time::SystemTime,
    ) -> Result<rustls::client::ServerCertVerified, rustls::Error> {
        Ok(rustls::client::ServerCertVerified::assertion())
    }
}
