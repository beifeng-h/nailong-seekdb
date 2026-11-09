@echo off
setlocal
set "SOURCE=%~dp0.."
set "OUTDIR=%~1"
if not defined OUTDIR exit /b 2
set "VSDEVCMD=%~2"
if not defined VSDEVCMD set "VSDEVCMD=C:\Program Files\Microsoft Visual Studio\18\Community\Common7\Tools\VsDevCmd.bat"
if not exist "%OUTDIR%" mkdir "%OUTDIR%"
if errorlevel 1 exit /b 2
call "%VSDEVCMD%" -no_logo -arch=x64 -host_arch=x64
if errorlevel 1 exit /b %errorlevel%
pushd "%OUTDIR%"
cl.exe /nologo /std:c++20 /EHsc /W4 /WX /utf-8 /O2 /I"%SOURCE%\include" /c "%SOURCE%\src\page_store.cpp" /Fo:page_store.obj
if errorlevel 1 goto failure
cl.exe /nologo /std:c++20 /EHsc /W4 /WX /utf-8 /O2 /I"%SOURCE%\include" /c "%SOURCE%\src\database.cpp" /Fo:database.obj
if errorlevel 1 goto failure
cl.exe /nologo /std:c++20 /EHsc /W4 /WX /utf-8 /O2 /I"%SOURCE%\include" /c "%SOURCE%\src\sql_parser.cpp" /Fo:sql_parser.obj
if errorlevel 1 goto failure
cl.exe /nologo /std:c++20 /EHsc /W4 /WX /utf-8 /O2 /I"%SOURCE%\include" /c "%SOURCE%\src\sql_executor.cpp" /Fo:sql_executor.obj
if errorlevel 1 goto failure
cl.exe /nologo /std:c++20 /EHsc /W4 /WX /utf-8 /O2 /I"%SOURCE%\include" "%SOURCE%\tests\storage_tests.cpp" page_store.obj /Fe:storage_tests.exe
if errorlevel 1 goto failure
cl.exe /nologo /std:c++20 /EHsc /W4 /WX /utf-8 /O2 /I"%SOURCE%\include" "%SOURCE%\tests\storage_fault_driver.cpp" page_store.obj /Fe:storage_fault_driver.exe
if errorlevel 1 goto failure
cl.exe /nologo /std:c++20 /EHsc /W4 /WX /utf-8 /O2 /I"%SOURCE%\include" "%SOURCE%\tests\database_tests.cpp" page_store.obj database.obj /Fe:database_tests.exe
if errorlevel 1 goto failure
cl.exe /nologo /std:c++20 /EHsc /W4 /WX /utf-8 /O2 /I"%SOURCE%\include" "%SOURCE%\tests\database_fault_driver.cpp" page_store.obj database.obj /Fe:database_fault_driver.exe
if errorlevel 1 goto failure
cl.exe /nologo /std:c++20 /EHsc /W4 /WX /utf-8 /O2 /I"%SOURCE%\include" "%SOURCE%\tests\sql_tests.cpp" page_store.obj database.obj sql_parser.obj sql_executor.obj /Fe:sql_tests.exe
if errorlevel 1 goto failure
cl.exe /nologo /std:c++20 /EHsc /W4 /WX /utf-8 /O2 /I"%SOURCE%\include" "%SOURCE%\tests\sql_fault_driver.cpp" page_store.obj database.obj sql_parser.obj sql_executor.obj /Fe:sql_fault_driver.exe
if errorlevel 1 goto failure
cl.exe /nologo /std:c++20 /EHsc /W4 /WX /utf-8 /O2 /I"%SOURCE%\include" "%SOURCE%\tests\sql_reference_tests.cpp" page_store.obj database.obj sql_parser.obj sql_executor.obj /Fe:sql_reference_tests.exe
if errorlevel 1 goto failure
cl.exe /nologo /std:c++20 /EHsc /W4 /WX /utf-8 /O2 /I"%SOURCE%\include" "%SOURCE%\tests\sql_public_case_driver.cpp" page_store.obj database.obj sql_parser.obj sql_executor.obj /Fe:sql_public_case_driver.exe
if errorlevel 1 goto failure
cl.exe /nologo /std:c++20 /EHsc /W4 /WX /utf-8 /O2 /I"%SOURCE%\include" "%SOURCE%\tests\vector_index_tests.cpp" page_store.obj database.obj sql_parser.obj sql_executor.obj /Fe:vector_index_tests.exe
if errorlevel 1 goto failure
cl.exe /nologo /std:c++20 /EHsc /W4 /WX /utf-8 /O2 /I"%SOURCE%\include" "%SOURCE%\tests\file_io_tests.cpp" /Fe:file_io_tests.exe
if errorlevel 1 goto failure
popd
exit /b 0
:failure
set "RESULT=%errorlevel%"
popd
exit /b %RESULT%
