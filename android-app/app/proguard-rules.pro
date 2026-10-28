# Keep Room entities and the WorkManager worker class names intact.
-keep class dev.smsrelay.data.** { *; }
-keep class dev.smsrelay.work.** { *; }
-keepclassmembers class * extends androidx.room.RoomDatabase { public *; }
