# Nithi Bot Android

Native Android companion for the Nithi Bot trading dashboard. The app uses the
same authenticated server APIs as the web dashboard and includes Today,
Performance, Paper, Exposure, Bot Config, API Accounts, Logs and Trend Engine.

## Release build

```powershell
flutter pub get
flutter analyze
flutter test
flutter build apk --release
```

The signed/unsigned release artifact (depending on the local Android signing
configuration) is written to `build/app/outputs/flutter-apk/app-release.apk`.
