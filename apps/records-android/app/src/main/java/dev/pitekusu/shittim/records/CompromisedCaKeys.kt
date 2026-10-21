package dev.pitekusu.shittim.records

import java.security.cert.X509Certificate
import javax.net.ssl.SSLPeerUnverifiedException
import okhttp3.CertificatePinner
import okhttp3.Interceptor
import okhttp3.Response

/** Extra rejection policy; platform trust, hostname verification, and CT remain in OkHttp. */
internal object CompromisedCaKeys : Interceptor {
  // Public incident inventory, including the code-signing keys and Sectigo cross-signs:
  // https://emudhra.we-have-a-problem.com/impacted-intermediates.json
  // Android's emergency blocklist also ships through Play Services. Keep this app-local
  // safeguard for devices that have not received it; no fixed snapshot of trusted roots.
  private val rejectedKeys = setOf(
    "sha256/6+1kdcLC2A/LGZ8/6N1uazzmC3frFdTkLD3sDG9bUQU=", // EM DV TLS CA - G3B
    "sha256/Q7/6GdbvZRRR9oQeAZkx6W9vLq7/KKtLbxzkV23icXs=", // EM OV TLS CA - G3B
    "sha256/4CVMf83CZeLw+KQo2rt1q0rgObSHLWL17lfwh31aikE=", // EM EV TLS CA - G3B
    "sha256/Sd01PcK5GaN3BkURfQ5gwWEV59G8JZ7v5zSwDqQ0/l8=", // EM DV TLS CA - G3C
    "sha256/Y+zv7kMdSVI7wRNsTnLQIGpHcIbK9gbNXPAlaGwQnPg=", // EM OV TLS CA - G3C
    "sha256/XAZgorFkkQ2P9YnBjhRVLp4Owjfc7+LLc21BpIEfgaA=", // EM EV TLS CA - G3C
    "sha256/7E/6GtCsiAgTFLeq/+9qkfl81XKRu1umhsVBX1609rY=", // EM OV CS CA - G3B
    "sha256/ksBj1n6PFNtx0ojhEmb/2vacvxFfe6sU87wvaNJeDjA=", // EM OV CS CA - G3C
  )

  override fun intercept(chain: Interceptor.Chain): Response {
    // A network interceptor runs after TLS/hostname verification but before HTTP data
    // is sent, including on reused/coalesced connections. OkHttp's Handshake exposes
    // the cleaned, verified chain, not just the intermediates supplied by the peer.
    // This covers alternate cross-signs and intermediates loaded from platform caches.
    val certificates = chain.connection()?.handshake()?.peerCertificates
    if (!chain.request().url.isHttps || certificates.isNullOrEmpty() || certificates.any {
      it !is X509Certificate || CertificatePinner.pin(it) in rejectedKeys
    }) {
      throw SSLPeerUnverifiedException("server_certificate_untrusted")
    }
    return chain.proceed(chain.request())
  }
}
