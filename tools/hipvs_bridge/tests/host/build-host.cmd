@echo off
setlocal
set "TASK=%~dp0"
set "BRIDGE=%~1"
if not defined BRIDGE set "BRIDGE=%TASK%..\..\seekdb_cuvs_bridge.c"
set "OUTDIR=%~2"
if not defined OUTDIR set "OUTDIR=%TEMP%\seekdb-cuvs-host-%RANDOM%-%RANDOM%"
for %%I in ("%OUTDIR%") do set "OUTDIR=%%~fI"
for %%I in ("%BRIDGE%") do set "BRIDGE=%%~fI"
for %%I in ("%BRIDGE%") do set "BRIDGEDIR=%%~dpI"
set "VSDEVCMD=%~3"
if not defined VSDEVCMD set "VSDEVCMD=C:\Program Files\Microsoft Visual Studio\18\Community\Common7\Tools\VsDevCmd.bat"
if not exist "%OUTDIR%" mkdir "%OUTDIR%"
if errorlevel 1 exit /b 2
call "%VSDEVCMD%" -no_logo -arch=x64 -host_arch=x64
if errorlevel 1 exit /b %errorlevel%
pushd "%OUTDIR%"
cl.exe /nologo /std:c++17 /D__HIP_PLATFORM_AMD__ /EHsc /W4 /TP /I"%TASK%include" /I"%BRIDGEDIR%." "%BRIDGE%" "%TASK%host_test.cpp" /Fe:"%OUTDIR%\host_test.exe"
set "RESULT=%errorlevel%"
popd
exit /b %RESULT%
