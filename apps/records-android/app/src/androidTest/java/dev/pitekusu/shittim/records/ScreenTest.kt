package dev.pitekusu.shittim.records

/** Compose screen and interaction coverage, selected with AndroidJUnitRunner's annotation filter. */
@Target(AnnotationTarget.CLASS, AnnotationTarget.FUNCTION)
@Retention(AnnotationRetention.RUNTIME)
annotation class ScreenTest
