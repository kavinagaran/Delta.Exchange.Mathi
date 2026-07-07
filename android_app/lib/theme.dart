import 'package:flutter/material.dart';

class AppColors {
  static const bg     = Color(0xFF080C1F);
  static const card   = Color(0xFF0D1130);
  static const lift   = Color(0xFF111630);
  static const border = Color(0xFF1E2748);
  static const text   = Color(0xFFE8EEFF);
  static const sub    = Color(0xFF6B7FA8);
  static const green  = Color(0xFF00E896);
  static const red    = Color(0xFFFF4560);
  static const blue   = Color(0xFF4E9EFF);
  static const purple = Color(0xFFA259FF);
  static const gold   = Color(0xFFFFD447);
  static const cyan   = Color(0xFF00E5FF);
}

class AppTheme {
  static ThemeData dark() => ThemeData(
    useMaterial3: true,
    colorScheme: ColorScheme.dark(
      surface: AppColors.card,
      onSurface: AppColors.text,
      primary: AppColors.blue,
      secondary: AppColors.cyan,
      error: AppColors.red,
    ),
    scaffoldBackgroundColor: AppColors.bg,
    cardTheme: CardTheme(
      color: AppColors.card,
      elevation: 0,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(12),
        side: const BorderSide(color: AppColors.border),
      ),
    ),
    navigationBarTheme: NavigationBarThemeData(
      backgroundColor: AppColors.card,
      surfaceTintColor: Colors.transparent,
      indicatorColor: AppColors.blue.withOpacity(0.18),
      iconTheme: MaterialStateProperty.resolveWith((states) => IconThemeData(
        color: states.contains(MaterialState.selected) ? AppColors.cyan : AppColors.sub,
      )),
      labelTextStyle: MaterialStateProperty.all(
        const TextStyle(color: AppColors.sub, fontSize: 11),
      ),
    ),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: AppColors.lift,
      border: OutlineInputBorder(
        borderRadius: BorderRadius.circular(8),
        borderSide: const BorderSide(color: AppColors.border),
      ),
      enabledBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(8),
        borderSide: const BorderSide(color: AppColors.border),
      ),
      focusedBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(8),
        borderSide: const BorderSide(color: AppColors.blue, width: 1.5),
      ),
      labelStyle: const TextStyle(color: AppColors.sub, fontSize: 13),
      hintStyle: const TextStyle(color: AppColors.sub),
    ),
    dividerTheme: const DividerThemeData(color: AppColors.border),
    appBarTheme: const AppBarTheme(
      backgroundColor: AppColors.card,
      foregroundColor: AppColors.text,
      elevation: 0,
      surfaceTintColor: Colors.transparent,
      titleTextStyle: TextStyle(
        color: AppColors.text,
        fontSize: 16,
        fontWeight: FontWeight.w600,
        letterSpacing: 0.5,
      ),
    ),
    snackBarTheme: const SnackBarThemeData(
      behavior: SnackBarBehavior.floating,
      shape: StadiumBorder(),
    ),
  );
}
