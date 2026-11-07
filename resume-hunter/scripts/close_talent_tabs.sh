#!/bin/bash
# 关闭 Chrome 里自动打开的人才页标签（URL 含 /hire/talent/）
osascript <<'APPLESCRIPT'
tell application "Google Chrome"
    set closedCount to 0
    repeat with w in windows
        set idxList to {}
        repeat with i from (count of tabs of w) to 1 by -1
            try
                set u to URL of tab i of w
                if (u contains "/hire/talent/") or (u contains "hire%2Ftalent%2F") then set end of idxList to i
            end try
        end repeat
        repeat with i in idxList
            try
                close tab (i as integer) of w
                set closedCount to closedCount + 1
            end try
        end repeat
    end repeat
    return closedCount
end tell
APPLESCRIPT
