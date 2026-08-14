@echo off
rem Action of the engine-control-observatory Scheduled Task (no time limit,
rem registered from scripts\observatory-task.xml): hosts the Observatory
rem server with its ngrok tunnel beside it. Started on demand by
rem control.start_observatory() (/resume) via hidden.vbs, so nothing
rem flashes. Stopping the task (schtasks /end) stops both.
start "" /b cmd /d /c "ngrok http 8010 > %TEMP%\ngrok-observatory.log 2>&1"
call C:\github\internet\ops\observatory.cmd
