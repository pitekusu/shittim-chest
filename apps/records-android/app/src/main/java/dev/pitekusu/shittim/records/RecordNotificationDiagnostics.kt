package dev.pitekusu.shittim.records

import com.google.firebase.installations.FirebaseInstallationsException
import dev.pitekusu.shittim.records.auth.MobileAuthException
import dev.pitekusu.shittim.records.auth.MobileAuthFailure
import dev.pitekusu.shittim.records.auth.TokenStorageException
import java.io.IOException

internal enum class NotificationRegistrationStage(val label: Int) {
  SESSION(R.string.notification_stage_session),
  FIREBASE(R.string.notification_stage_firebase),
  REGISTER(R.string.notification_stage_register),
  UNREGISTER(R.string.notification_stage_unregister),
  EXPIRY(R.string.notification_stage_expiry),
  SAVE(R.string.notification_stage_save),
}

internal enum class NotificationRegistrationFailure(val label: Int, val retryable: Boolean = false) {
  NETWORK(R.string.notification_failure_network, true),
  THROTTLED(R.string.notification_failure_throttled, true),
  UNAVAILABLE(R.string.notification_failure_unavailable, true),
  RESPONSE(R.string.notification_failure_response),
  AUTHORIZATION(R.string.notification_failure_authorization),
  EXPIRY(R.string.notification_failure_expiry),
  STORAGE(R.string.notification_failure_storage),
  FIREBASE(R.string.notification_failure_firebase, true),
  CONFIGURATION(R.string.notification_failure_configuration),
  UNEXPECTED(R.string.notification_failure_unexpected),
}

/** Allowlisted categories only. No exception message, cause, credentials or device identifiers. */
internal fun notificationRegistrationFailure(stage: NotificationRegistrationStage, error: Exception):
  NotificationRegistrationFailure = when {
    error is MobileAuthException -> when (error.failure) {
      MobileAuthFailure.NETWORK -> NotificationRegistrationFailure.NETWORK
      MobileAuthFailure.THROTTLED -> NotificationRegistrationFailure.THROTTLED
      MobileAuthFailure.UNAVAILABLE -> NotificationRegistrationFailure.UNAVAILABLE
      MobileAuthFailure.AUTHENTICATION_REQUIRED, MobileAuthFailure.FORBIDDEN -> NotificationRegistrationFailure.AUTHORIZATION
      else -> NotificationRegistrationFailure.RESPONSE
    }
    error is TokenStorageException -> NotificationRegistrationFailure.STORAGE
    error is FirebaseInstallationsException -> when (error.status) {
      FirebaseInstallationsException.Status.BAD_CONFIG -> NotificationRegistrationFailure.CONFIGURATION
      FirebaseInstallationsException.Status.UNAVAILABLE -> NotificationRegistrationFailure.UNAVAILABLE
      FirebaseInstallationsException.Status.TOO_MANY_REQUESTS -> NotificationRegistrationFailure.THROTTLED
    }
    error is IOException -> NotificationRegistrationFailure.NETWORK
    stage == NotificationRegistrationStage.EXPIRY -> NotificationRegistrationFailure.EXPIRY
    stage == NotificationRegistrationStage.SAVE -> NotificationRegistrationFailure.STORAGE
    stage == NotificationRegistrationStage.FIREBASE ->
      if (error is IllegalStateException || error is IllegalArgumentException) NotificationRegistrationFailure.CONFIGURATION
      else NotificationRegistrationFailure.FIREBASE
    else -> NotificationRegistrationFailure.UNEXPECTED
  }
