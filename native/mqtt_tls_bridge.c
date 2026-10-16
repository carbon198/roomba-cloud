/*
 * Per-connection TLS adapter for mqtt_probe.ClientCertificateTransport.
 * Build: cc -O2 -Wall -Wextra -Werror native/mqtt_tls_bridge.c -o tools/mqtt_tls_bridge
 *        -lssl -lcrypto
 *
 * The tested V4 client needs a TLS 1.2 CertificateRequest offering RSA PKCS#1
 * SHA-256. Native OpenSSL exposes that control; Python's ssl API does not expose
 * the same client-signature selection. This is compatibility, NOT robot
 * authentication: the callback accepts any issuer and no certificate is required.
 * The robot must still trust our server chain. Keep this service on a trusted LAN.
 *
 * Parent passes one accepted socket FD and private PEM paths. Stdout begins with
 * one JSON handshake-metadata line, then contains only decrypted MQTT bytes.
 * Stdin contains MQTT bytes to encrypt; stderr contains transport diagnostics.
 * Never print plaintext packets or certificate/key contents into metadata/logs.
 * EOF/timeout ends this connection, not the listening server.
 */
#include <openssl/err.h>
#include <openssl/ssl.h>
#include <errno.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

static int allow_untrusted_client(int valid, X509_STORE_CTX *store)
{
    (void)valid;
    (void)store;
    return 1;
}

static int failure(const char *operation)
{
    unsigned long code = ERR_get_error();
    const char *reason = code ? ERR_reason_error_string(code) : NULL;
    fprintf(stderr, "%s: %s\n", operation, reason ? reason : "TLS transport failure");
    return 1;
}

static int write_stdout(const unsigned char *data, size_t length)
{
    while (length) {
        ssize_t written = write(STDOUT_FILENO, data, length);
        if (written < 0 && errno == EINTR) continue;
        if (written <= 0) return -1;
        data += written;
        length -= (size_t)written;
    }
    return 0;
}

int main(int argc, char **argv)
{
    if (argc != 5) {
        fprintf(stderr, "usage: bridge socket-fd cert-chain key timeout-seconds\n");
        return 2;
    }
    int fd = atoi(argv[1]);
    struct timeval timeout = { .tv_sec = atoi(argv[4]) };
    if (fd < 0 || timeout.tv_sec <= 0) return 2;
    if (setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) ||
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout))) {
        perror("socket timeout");
        return 1;
    }
    SSL_CTX *context = SSL_CTX_new(TLS_server_method());
    if (!context) return failure("context");
    int result = 1;
    SSL *tls = NULL;
    if (!SSL_CTX_set_min_proto_version(context, TLS1_2_VERSION) ||
        !SSL_CTX_set_max_proto_version(context, TLS1_2_VERSION) ||
        !SSL_CTX_set1_client_sigalgs_list(context, "rsa_pkcs1_sha256") ||
        !SSL_CTX_use_certificate_chain_file(context, argv[2]) ||
        !SSL_CTX_use_PrivateKey_file(context, argv[3], SSL_FILETYPE_PEM) ||
        !SSL_CTX_check_private_key(context)) {
        failure("configuration");
        goto cleanup;
    }
    /* Request a compatible client signature, but do not authenticate its issuer. */
    SSL_CTX_set_verify(context, SSL_VERIFY_PEER, allow_untrusted_client);
    SSL_CTX_set_verify_depth(context, 10);
    tls = SSL_new(context);
    if (!tls || !SSL_set_fd(tls, fd) || SSL_accept(tls) != 1) {
        failure("handshake");
        goto cleanup;
    }
    X509 *client = SSL_get1_peer_certificate(tls);
    int signature_type = NID_undef;
    int signature_digest = NID_undef;
    if (client &&
        (!SSL_get_peer_signature_type_nid(tls, &signature_type) ||
         !SSL_get_peer_signature_nid(tls, &signature_digest))) {
        X509_free(client);
        failure("client signature metadata");
        goto cleanup;
    }
    printf("{\"tls\":\"%s\",\"cipher\":\"%s\",\"client_certificate_present\":%s,"
           "\"client_signature_type\":\"%s\",\"client_signature_digest\":\"%s\"}\n",
           SSL_get_version(tls), SSL_get_cipher_name(tls), client ? "true" : "false",
           client ? OBJ_nid2sn(signature_type) : "none",
           client ? OBJ_nid2sn(signature_digest) : "none");
    X509_free(client);
    if (fflush(stdout)) {
        perror("handshake metadata");
        goto cleanup;
    }
    unsigned char buffer[8192];
    struct pollfd inputs[] = {
        { .fd = fd, .events = POLLIN },
        { .fd = STDIN_FILENO, .events = POLLIN },
    };
    /* SSL_pending includes decrypted records already buffered without FD readiness. */
    for (;;) {
        int ready = poll(inputs, 2, SSL_pending(tls) ? 0 : (int)timeout.tv_sec * 1000);
        if (ready < 0 && errno == EINTR) continue;
        if (ready < 0) {
            perror("poll");
            goto cleanup;
        }
        if (!ready && !SSL_pending(tls)) {
            fprintf(stderr, "transport: idle timeout\n");
            goto cleanup;
        }
        if (inputs[1].revents & (POLLIN | POLLHUP)) {
            ssize_t count = read(STDIN_FILENO, buffer, sizeof(buffer));
            if (count < 0 && errno == EINTR) continue;
            if (count < 0) {
                perror("stdin");
                goto cleanup;
            }
            if (!count) {
                SSL_shutdown(tls);
                result = 0;
                break;
            }
            ssize_t offset = 0;
            while (offset < count) {
                int sent = SSL_write(tls, buffer + offset, (int)(count - offset));
                if (sent <= 0) {
                    failure("TLS write");
                    goto cleanup;
                }
                offset += sent;
            }
        }
        if (SSL_pending(tls) || inputs[0].revents & (POLLIN | POLLHUP | POLLERR)) {
            int count = SSL_read(tls, buffer, sizeof(buffer));
            if (count <= 0) {
                if (SSL_get_error(tls, count) == SSL_ERROR_ZERO_RETURN) {
                    SSL_shutdown(tls);
                    result = 0;
                    break;
                }
                failure("TLS read");
                goto cleanup;
            }
            if (write_stdout(buffer, (size_t)count)) {
                perror("stdout");
                goto cleanup;
            }
        }
    }
cleanup:
    SSL_free(tls);
    SSL_CTX_free(context);
    close(fd);
    return result;
}
