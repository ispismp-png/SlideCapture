// 버전은 여기 한 곳(APP_VERSION)에서만 관리: 앱 헤더·PPT 기본 파일명·APK 이름에 모두 반영
val appVersion = "v1.6"
val appVersionCode = 16

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

android {
    namespace = "com.slidecapture.pro"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.slidecapture.pro"
        minSdk = 29
        targetSdk = 34
        versionCode = appVersionCode
        versionName = appVersion
        ndk {
            abiFilters += listOf("arm64-v8a")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("debug")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        buildConfig = true
    }
    androidResources {
        noCompress += listOf("traineddata", "ttf")
    }
}

chaquopy {
    defaultConfig {
        version = "3.8"
        pip {
            install("numpy")
            install("opencv-python")
            install("pillow")
            install("lxml")
            install("python-pptx")
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.12.0")
    implementation("androidx.appcompat:appcompat:1.6.1")
    implementation("com.google.android.material:material:1.11.0")
    implementation("androidx.activity:activity-ktx:1.8.2")
    implementation("androidx.recyclerview:recyclerview:1.3.2")
    implementation("androidx.exifinterface:exifinterface:1.3.7")
    val camerax = "1.3.4"
    implementation("androidx.camera:camera-core:$camerax")
    implementation("androidx.camera:camera-camera2:$camerax")
    implementation("androidx.camera:camera-lifecycle:$camerax")
    implementation("androidx.camera:camera-view:$camerax")
    implementation("cz.adaptech.tesseract4android:tesseract4android:4.7.0")
}
