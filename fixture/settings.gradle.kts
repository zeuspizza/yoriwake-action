rootProject.name = "demo"

// The plugin under test: the released version by default, or the version a workflow job built
// from source and published to the local Maven repository (`-PyoriwakeVersion=...`).
pluginManagement {
    val yoriwakeVersion = providers.gradleProperty("yoriwakeVersion").getOrElse("0.1.0")
    repositories {
        mavenLocal()
        gradlePluginPortal()
    }
    plugins {
        id("io.github.zeuspizza.yoriwake") version yoriwakeVersion
    }
}
