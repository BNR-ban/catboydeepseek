@echo off
rem Launch the companion from a source checkout on Windows.
setlocal
set HERE=%~dp0
if "%PYTHONPATH%"=="" (
  set PYTHONPATH=%HERE%src
) else (
  set PYTHONPATH=%HERE%src;%PYTHONPATH%
)
rem Qt's raster engine is all we need; this keeps a software GL stack out.
if "%QT_XCB_GL_INTEGRATION%"=="" set QT_XCB_GL_INTEGRATION=none
"%PYTHON%" -m dscompanion %*
if "%PYTHON%"=="" python -m dscompanion %*
