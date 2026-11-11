# Timers and alarms

Ask in chat: “Set a timer for 5 minutes”, “Set a timer for 1 hour 30 minutes”, “Set an alarm tomorrow at 7:30 AM”, or “Set an alarm at 23:30”. These actions are registered locally without model inference or an external API.
Timer cards count down inside chat and provide Pause, Resume, and Cancel. Ask “List timers” to see active timers.
Alarms use the device time zone. Without a date, the next occurrence is used, including tomorrow if that time has passed today. The card shows the target date and time and allows cancellation. Alarms cannot be paused because their time is fixed. Ask “List alarms” to see them.
Timers support 1 second through 7 days; alarms are one-shot and support times within the next 7 days. For multiple items, use their individual cards or give them names in quotes.
Allow macOS notifications on first use. If denied, enable OLCR in System Settings → Notifications and return to OLCR. Notifications with sound are scheduled with macOS. Once scheduled, they can appear even after leaving the chat or quitting OLCR. On-time display is not guaranteed while the Mac is asleep or powered off.
Records persist locally. Follow any scheduling, failure, or permission message on the card. Successful scheduling does not mean a notification has already been delivered.

Short corrections such as “today at 6”, “PM”, or “tomorrow” apply to the preceding alarm. Ambiguous times prompt for AM/PM or a date, and the same alarm is updated. Stopped or finished alarms require confirmation before restarting. Timer corrections can change or extend the duration or rename it; editing a paused timer keeps it paused. Short time/status questions refer to the preceding item.
