@echo off
:: Manual proof-of-concept for the P2 transcode pipeline.
::
:: Step 1: Generate a 10s 720p30 HLS test source with video (testsrc pattern)
::         + audio (1kHz sine wave) using lavfi virtual inputs.
:: Step 2: Re-segment that HLS through a no-op video filter (-vf "null") and
::         AAC audio passthrough, mirroring what the P2 transcoder will do.
:: Step 3: Open the result in VLC for visual confirmation.

setlocal
set "SRC=%TEMP%\castbooster_spike\src"
set "OUT=%TEMP%\castbooster_spike\out"

if exist "%SRC%" rmdir /s /q "%SRC%"
if exist "%OUT%" rmdir /s /q "%OUT%"
mkdir "%SRC%"
mkdir "%OUT%"

:: ---------- step 1: source ----------
echo === [1/3] Generating test HLS source at %SRC% ===
ffmpeg -y -hide_banner -loglevel warning ^
       -f lavfi -i "testsrc=size=1280x720:rate=30:duration=10" ^
       -f lavfi -i "sine=frequency=1000:duration=10" ^
       -c:v libx264 -preset ultrafast -g 60 ^
       -c:a aac -b:a 96k ^
       -hls_time 2 -hls_list_size 0 ^
       -hls_segment_filename "%SRC%\seg_%%03d.ts" ^
       -f hls "%SRC%\master.m3u8"
if errorlevel 1 (echo source generation failed & exit /b 1)

:: ---------- step 2: re-segment through noop ----------
echo === [2/3] Re-segmenting via noop filter to %OUT% ===
ffmpeg -y -hide_banner -loglevel warning ^
       -i "%SRC%\master.m3u8" ^
       -vf "null" ^
       -c:v libx264 -preset veryfast ^
       -c:a copy ^
       -hls_time 4 -hls_list_size 0 ^
       -hls_segment_filename "%OUT%\seg_%%03d.ts" ^
       -f hls "%OUT%\master.m3u8"
if errorlevel 1 (echo re-segment failed & exit /b 1)

:: ---------- step 3: open in VLC ----------
echo === [3/3] Opening output in default player ===
start "" "%OUT%\master.m3u8"

echo Done. Output dir: %OUT%
endlocal
