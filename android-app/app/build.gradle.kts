plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.google.devtools.ksp")
}

val stableDebugStore = System.getenv("ANDROID_KEYSTORE_PATH")

android {
    namespace = "dev.smsrelay"
    compileSdk = 35

    defaultConfig {
        applicationId = "dev.smsrelay"
        minSdk = 26
        targetSdk = 35
        versionCode = 7
        versionName = "1.3.2"
    }

    buildFeatures {
        buildConfig = true
    }

    // GitHub Actions supplies a stable debug signing key through secrets,
    // so a newly built APK can update the previous one. A local build
    // without the environment keeps the platform debug key.
    signingConfigs {
        if (!stableDebugStore.isNullOrBlank()) {
            create("stableDebug") {
                storeFile = file(stableDebugStore)
                storePassword = System.getenv("ANDROID_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("ANDROID_KEY_ALIAS")
                keyPassword = System.getenv("ANDROID_KEY_PASSWORD")
                storeType = "PKCS12"
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
        debug {
            isMinifyEnabled = false
            if (!stableDebugStore.isNullOrBlank()) {
                signingConfig = signingConfigs.getByName("stableDebug")
            }
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("androidx.recyclerview:recyclerview:1.3.2")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")

    implementation("androidx.room:room-runtime:2.6.1")
    implementation("androidx.room:room-ktx:2.6.1")
    ksp("androidx.room:room-compiler:2.6.1")

    implementation("androidx.work:work-runtime-ktx:2.9.1")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    // The QR code of the pairing is drawn on the phone, without a
    // network round trip.
    implementation("com.google.zxing:core:3.5.3")
}
