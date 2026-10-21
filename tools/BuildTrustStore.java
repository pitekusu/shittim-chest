// SPDX-License-Identifier: MIT
// Standard JDK bulk import for public CA certificates. No custom TLS verifier.
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.KeyStore;
import java.security.MessageDigest;
import java.security.cert.CertificateFactory;
import java.util.HexFormat;

class BuildTrustStore {
    public static void main(String[] args) throws Exception {
        var store = KeyStore.getInstance("PKCS12");
        store.load(null, null);
        try (var input = Files.newInputStream(Path.of(args[0]))) {
            var certificates = CertificateFactory.getInstance("X.509").generateCertificates(input);
            for (var certificate : certificates) {
                var fingerprint = HexFormat.of().formatHex(
                    MessageDigest.getInstance("SHA-256").digest(certificate.getEncoded()));
                store.setCertificateEntry(fingerprint, certificate);
            }
        }
        try (var output = Files.newOutputStream(Path.of(args[1]))) {
            // This file contains public certificates only. The password is not a secret.
            store.store(output, "public-only".toCharArray());
        }
    }
}
