use crate::error::{PortalError, PortalResult};
use log::error;
use quinn::{ClientConfig, Connection, Endpoint, RecvStream, SendStream, ServerConfig};
use quinn::congestion::{BbrConfig, CubicConfig};
use std::net::SocketAddr;
use std::sync::Arc;
use tokio::sync::Mutex;
use tokio::time::{timeout, Duration};

/// Configuration options for QUIC transport
#[derive(Debug, Clone)]
pub struct TransportOptions {
    pub max_idle_timeout_secs: u64,
    pub congestion_controller_type: String,
    pub initial_window: u64,
    pub keep_alive_interval_secs: u64,
}

impl Default for TransportOptions {
    fn default() -> Self {
        Self {
            max_idle_timeout_secs: 10,
            congestion_controller_type: "cubic".to_string(),
            initial_window: 1024 * 1024,
            keep_alive_interval_secs: 2,
        }
    }
}

impl TransportOptions {
    pub fn new() -> Self {
        Self::default()
    }
}

/// Shared transport configuration builder for consistent QUIC settings
struct TransportConfigBuilder;

impl TransportConfigBuilder {
    fn build(options: &TransportOptions) -> quinn::TransportConfig {
        let mut transport_config = quinn::TransportConfig::default();

        // Short timeouts, relying on keep-alive to keep connection fresh.
        transport_config
            .max_idle_timeout(Some(std::time::Duration::from_secs(options.max_idle_timeout_secs).try_into().unwrap()));

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

        match options.congestion_controller_type.as_str() {
            "fixed" => {
                transport_config.congestion_controller_factory(Arc::new(FixedWindowConfig::new(options.initial_window)));
            }
            "bbr" => {
                let mut bbr_config = BbrConfig::default();
                bbr_config.initial_window(options.initial_window);
                transport_config.congestion_controller_factory(Arc::new(bbr_config));
            }
            "cubic" => {
                let mut cubic_config = CubicConfig::default();
                cubic_config.initial_window(options.initial_window);
                transport_config.congestion_controller_factory(Arc::new(cubic_config));
            }
            _ => {
                // Default to cubic
                error!("Invalid congestion controller type: {}. Using default congestion controller: cubic", options.congestion_controller_type);
                let mut cubic_config = CubicConfig::default();
                cubic_config.initial_window(options.initial_window);
                transport_config.congestion_controller_factory(Arc::new(cubic_config));
            }
        }

        // Set minimum MTU to avoid fragmentation issues
        transport_config.min_mtu(1200); // Very conservative MTU for maximum compatibility

        // Disable MTU discovery to debug performance issue.
        transport_config.mtu_discovery_config(None);

        // Keep alive every 2s.
        transport_config.keep_alive_interval(Some(std::time::Duration::from_secs(options.keep_alive_interval_secs)));

        transport_config
    }
}

/// Configuration for fixed window congestion controller
#[derive(Debug, Clone)]
pub struct FixedWindowConfig {
    window_size: u64,
}

impl FixedWindowConfig {
    pub fn new(window_size: u64) -> Self {
        Self {
            window_size,
        }
    }
}

impl quinn::congestion::ControllerFactory for FixedWindowConfig {
    fn build(
        self: Arc<Self>,
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
                .map_err(|e| PortalError::QuicError(format!("Failed to send length: {:?}", e)))?;

            // Send data.
            // TODO: consider chunking.
            send.write_all(&data)
                .await
                .map_err(|e| PortalError::QuicError(format!("Failed to send data: {:?}", e)))?;

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
                    .map_err(|e| PortalError::QuicError(format!("Failed to read length: {:?}", e)))?;

                let data_length = u32::from_be_bytes(len_buf) as usize;

                // Read the complete message
                let mut data = vec![0u8; data_length];
                recv.read_exact(&mut data)
                    .await
                    .map_err(|e| PortalError::QuicError(format!("Failed to read data: {:?}", e)))?;

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
        if let Some(mut send) = self.send_stream.lock().await.take() {
            let _ = send.finish();
        }
        self.connection.close(0u32.into(), b"closed");
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
    pub async fn listen_and_accept(local_port: u16, transport_options: &TransportOptions) -> PortalResult<Self> {
        // Configure server
        let server_config = configure_server(transport_options)?;
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
    pub async fn connect(server_ip: &str, server_port: u16, local_port: u16, transport_options: &TransportOptions) -> PortalResult<Self> {
        // Configure client
        let client_config = configure_client(transport_options)?;
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

fn configure_client(transport_options: &TransportOptions) -> PortalResult<ClientConfig> {
    // Create rustls client config with insecure settings for simplicity
    let crypto = rustls::ClientConfig::builder_with_provider(
        rustls::crypto::ring::default_provider().into(),
    )
    .with_safe_default_protocol_versions()
    .map_err(|e| PortalError::QuicError(format!("Failed to create rustls client config: {}", e)))?
    .dangerous()
    .with_custom_certificate_verifier(Arc::new(SkipServerVerification))
    .with_no_client_auth();

    // Convert to Quinn's QuicClientConfig
    let client_crypto = quinn::crypto::rustls::QuicClientConfig::try_from(crypto).map_err(|e| {
        PortalError::QuicError(format!("Failed to create QUIC client config: {}", e))
    })?;

    let mut client_config = ClientConfig::new(Arc::new(client_crypto));
    client_config.transport_config(Arc::new(TransportConfigBuilder::build(transport_options)));

    Ok(client_config)
}

fn configure_server(transport_options: &TransportOptions) -> PortalResult<ServerConfig> {
    // Generate a self-signed certificate
    let cert = rcgen::generate_simple_self_signed(vec!["localhost".into()])
        .map_err(|e| PortalError::QuicError(format!("Failed to generate certificate: {}", e)))?;

    // Convert to the new rustls types
    let cert_der =
        rustls_pki_types::CertificateDer::from(cert.serialize_der().map_err(|e| {
            PortalError::QuicError(format!("Failed to serialize certificate: {}", e))
        })?);
    let private_key = rustls_pki_types::PrivateKeyDer::from(
        rustls_pki_types::PrivatePkcs8KeyDer::from(cert.get_key_pair().serialize_der()),
    );

    // Create rustls server config
    let crypto = rustls::ServerConfig::builder_with_provider(
        rustls::crypto::ring::default_provider().into(),
    )
    .with_safe_default_protocol_versions()
    .map_err(|e| PortalError::QuicError(format!("Failed to create rustls server config: {}", e)))?
    .with_no_client_auth()
    .with_single_cert(vec![cert_der], private_key)
    .map_err(|e| PortalError::QuicError(format!("Failed to set certificate: {}", e)))?;

    // Convert to Quinn's QuicServerConfig
    let server_crypto = quinn::crypto::rustls::QuicServerConfig::try_from(crypto).map_err(|e| {
        PortalError::QuicError(format!("Failed to create QUIC server config: {}", e))
    })?;

    let mut server_config = ServerConfig::with_crypto(Arc::new(server_crypto));
    server_config.transport_config(Arc::new(TransportConfigBuilder::build(transport_options)));

    Ok(server_config)
}

// Custom certificate verifier that accepts all certificates (for development only!)
#[derive(Debug)]
struct SkipServerVerification;

impl rustls::client::danger::ServerCertVerifier for SkipServerVerification {
    fn verify_server_cert(
        &self,
        _end_entity: &rustls_pki_types::CertificateDer<'_>,
        _intermediates: &[rustls_pki_types::CertificateDer<'_>],
        _server_name: &rustls_pki_types::ServerName<'_>,
        _ocsp_response: &[u8],
        _now: rustls_pki_types::UnixTime,
    ) -> Result<rustls::client::danger::ServerCertVerified, rustls::Error> {
        Ok(rustls::client::danger::ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        _message: &[u8],
        _cert: &rustls_pki_types::CertificateDer<'_>,
        _dss: &rustls::DigitallySignedStruct,
    ) -> Result<rustls::client::danger::HandshakeSignatureValid, rustls::Error> {
        Ok(rustls::client::danger::HandshakeSignatureValid::assertion())
    }

    fn verify_tls13_signature(
        &self,
        _message: &[u8],
        _cert: &rustls_pki_types::CertificateDer<'_>,
        _dss: &rustls::DigitallySignedStruct,
    ) -> Result<rustls::client::danger::HandshakeSignatureValid, rustls::Error> {
        Ok(rustls::client::danger::HandshakeSignatureValid::assertion())
    }

    fn supported_verify_schemes(&self) -> Vec<rustls::SignatureScheme> {
        vec![
            rustls::SignatureScheme::RSA_PKCS1_SHA1,
            rustls::SignatureScheme::ECDSA_SHA1_Legacy,
            rustls::SignatureScheme::RSA_PKCS1_SHA256,
            rustls::SignatureScheme::ECDSA_NISTP256_SHA256,
            rustls::SignatureScheme::RSA_PKCS1_SHA384,
            rustls::SignatureScheme::ECDSA_NISTP384_SHA384,
            rustls::SignatureScheme::RSA_PKCS1_SHA512,
            rustls::SignatureScheme::ECDSA_NISTP521_SHA512,
            rustls::SignatureScheme::RSA_PSS_SHA256,
            rustls::SignatureScheme::RSA_PSS_SHA384,
            rustls::SignatureScheme::RSA_PSS_SHA512,
            rustls::SignatureScheme::ED25519,
            rustls::SignatureScheme::ED448,
        ]
    }
}
