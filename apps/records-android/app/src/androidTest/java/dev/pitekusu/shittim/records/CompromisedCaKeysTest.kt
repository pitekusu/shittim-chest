package dev.pitekusu.shittim.records

import android.net.http.X509TrustManagerExtensions
import android.os.Bundle
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import java.lang.reflect.Proxy
import java.security.KeyStore
import java.security.cert.CertPathBuilder
import java.security.cert.CertStore
import java.security.cert.CertificateFactory
import java.security.cert.CollectionCertStoreParameters
import java.security.cert.PKIXBuilderParameters
import java.security.cert.PKIXCertPathBuilderResult
import java.security.cert.TrustAnchor
import java.security.cert.X509CertSelector
import java.security.cert.X509Certificate
import java.time.Instant
import java.util.Date
import javax.net.ssl.SSLPeerUnverifiedException
import javax.net.ssl.TrustManagerFactory
import javax.net.ssl.X509TrustManager
import okhttp3.CipherSuite
import okhttp3.Connection
import okhttp3.Handshake
import okhttp3.Interceptor
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Request
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import okhttp3.TlsVersion
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class CompromisedCaKeysTest {
  private val assets get() = InstrumentationRegistry.getInstrumentation().context.assets

  @Test fun allTwelveOtherwiseValidIncidentPathsAreRejectedBeforeHttp() {
    val files = assets.list("tls-emudhra")!!.filter { it.endsWith("we-have-a-problem.com.pem") }
    assertEquals(12, files.size)
    for (file in files) {
      val supplied = certificates(file)
      val verified = buildPath(supplied.first(), supplied.drop(1))
      assertTrue(verified.size >= 3)
      assertRejectedBeforeHttp(verified)
    }
  }

  @Test fun allEightKeysAreBlockedIncludingAlternateCertificateRepresentations() {
    val variants = certificates("compromised-intermediates.pem")
    assertEquals(16, variants.size)
    assertEquals(8, variants.map { it.publicKey.encoded.toList() }.distinct().size)
    for (certificate in variants) assertRejectedBeforeHttp(listOf(certificate))
  }

  @Test fun cachedIntermediateInTheVerifiedPathCannotBypassTheBlock() {
    val supplied = certificates("dv-g3b-aaa-emudhra.we-have-a-problem.com.pem")
    // Only the leaf is the target; the builder must recover the issuer from its
    // separate CertStore, like the verified chain exposed by Android/OkHttp.
    val recovered = buildPath(supplied.first(), supplied.drop(1))
    assertTrue(recovered.drop(1).contains(supplied[1]))
    assertRejectedBeforeHttp(recovered)
  }

  @Test fun normalVerifiedChainKeepsTheRequestAndResponseUnchanged() {
    val supplied = certificates("normal-chain.pem")
    val verified = buildPath(supplied.first(), supplied.drop(1))
    val probe = Probe(verified)
    val response = CompromisedCaKeys.intercept(probe.chain)
    assertTrue(probe.proceeded)
    assertTrue(probe.response === response)
    assertTrue(probe.request === response.request)
    response.close()
    if (InstrumentationRegistry.getArguments().getString("shittimTlsLiveControl") == "true") {
      liveNormalTlsControlUsesPlatformValidationAndTheGuard()
    }
  }

  @Test fun absentOrEmptyHandshakeFailsClosed() {
    assertRejectedBeforeHttp(emptyList())
    val probe = Probe(null)
    try {
      CompromisedCaKeys.intercept(probe.chain)
      fail("TLS handshake required")
    } catch (expected: SSLPeerUnverifiedException) {
      assertEquals("server_certificate_untrusted", expected.message)
    }
    assertFalse(probe.proceeded)
  }

  @Test fun platformVerifiedPathsAreRejectedEvenBeforeTheDeviceReceivesItsUpdate() {
    val factory = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm()).apply {
      init(null as KeyStore?)
    }
    val trust = factory.trustManagers.filterIsInstance<X509TrustManager>().single()
    val extensions = X509TrustManagerExtensions(trust)
    var platformAccepted = 0
    var platformRejected = 0
    for (file in assets.list("tls-emudhra")!!.filter { it.endsWith("we-have-a-problem.com.pem") }) {
      val supplied = certificates(file)
      val verified = try {
        extensions.checkServerTrusted(supplied.toTypedArray(), "RSA", file.removeSuffix(".pem"))
      } catch (_: java.security.cert.CertificateException) {
        platformRejected++
        continue
      }
      platformAccepted++
      assertRejectedBeforeHttp(verified)
      // Reuse the same platform TrustManager after its issuer cache was populated,
      // and supply just the leaf as a server could do on a subsequent connection.
      assertRejectedBeforeHttp(extensions.checkServerTrusted(arrayOf(supplied.first()), "RSA",
        file.removeSuffix(".pem")))
    }
    assertEquals(12, platformAccepted + platformRejected)
    InstrumentationRegistry.getInstrumentation().sendStatus(0, Bundle().apply {
      putString("tls_platform_incident_paths", "accepted=$platformAccepted rejected=$platformRejected app_blocked=$platformAccepted")
    })
  }

  private fun liveNormalTlsControlUsesPlatformValidationAndTheGuard() {
    // Explicit local smoke extends the mandatory offline positive control.
    val client = OkHttpClient.Builder().addNetworkInterceptor(CompromisedCaKeys).build()
    try {
      val request = Request.Builder().url("https://www.python.org/").head().build()
      client.newCall(request).execute().use { response ->
        assertTrue(response.isSuccessful)
        assertTrue(response.handshake!!.peerCertificates.isNotEmpty())
      }
    } finally {
      client.connectionPool.evictAll()
      client.dispatcher.executorService.shutdown()
    }
  }

  private fun certificates(name: String): List<X509Certificate> =
    assets.open("tls-emudhra/$name").use { input ->
      CertificateFactory.getInstance("X.509").generateCertificates(input).map { it as X509Certificate }
    }

  private fun buildPath(leaf: X509Certificate, stored: List<X509Certificate>): List<X509Certificate> {
    val roots = certificates("isolated-test-roots.pem").map { TrustAnchor(it, null) }.toSet()
    val selector = X509CertSelector().apply { certificate = leaf }
    val parameters = PKIXBuilderParameters(roots, selector).apply {
      // Offline reproduction of a store before vendor revocation updates. This
      // is not a production TrustManager or an override of app TLS validation.
      isRevocationEnabled = false
      date = Date.from(Instant.parse("2026-10-21T00:00:00Z"))
      addCertStore(CertStore.getInstance("Collection", CollectionCertStoreParameters(listOf(leaf) + stored)))
    }
    val result = CertPathBuilder.getInstance("PKIX").build(parameters) as PKIXCertPathBuilderResult
    return result.certPath.certificates.map { it as X509Certificate } + result.trustAnchor.trustedCert
  }

  private fun assertRejectedBeforeHttp(certificates: List<X509Certificate>) {
    val probe = Probe(certificates)
    try {
      CompromisedCaKeys.intercept(probe.chain)
      fail("compromised path accepted")
    } catch (expected: SSLPeerUnverifiedException) {
      assertEquals("server_certificate_untrusted", expected.message)
    }
    assertFalse(probe.proceeded)
  }

  private class Probe(certificates: List<X509Certificate>?) {
    val request = Request.Builder().url("https://example.invalid/").build()
    val response = Response.Builder().request(request).protocol(Protocol.HTTP_1_1)
      .code(200).message("OK").body("control".toResponseBody()).build()
    var proceeded = false
    private val handshake = certificates?.let {
      Handshake.get(TlsVersion.TLS_1_3, CipherSuite.TLS_AES_128_GCM_SHA256, it, emptyList())
    }
    private val connection = Proxy.newProxyInstance(Connection::class.java.classLoader,
      arrayOf(Connection::class.java)) { _, method, _ ->
      when (method.name) {
        "handshake" -> handshake
        else -> error("unexpected connection method")
      }
    } as Connection
    val chain = Proxy.newProxyInstance(Interceptor.Chain::class.java.classLoader,
      arrayOf(Interceptor.Chain::class.java)) { _, method, args ->
      when (method.name) {
        "request" -> request
        "connection" -> connection
        "proceed" -> {
          assertTrue(request === args!![0])
          proceeded = true
          response
        }
        else -> error("unexpected chain method")
      }
    } as Interceptor.Chain
  }
}
