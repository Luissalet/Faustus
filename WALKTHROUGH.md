# Delivery walkthrough

The finished turn summary offers **Vídeo-recorrido**. Give the running app's HTTP address and one caption per view. Recording starts with the first caption, then scrolls the real page to each following view. The inline player and download link appear when Prospero finishes. The app must be reachable from the Prospero server; its browser context starts without the person's cookies or existing login.

For a journey that follows links and specific controls, the native and MCP `task_walkthrough` tool accepts the same explicit steps as Prospero's `website_walkthrough`:

```json
{"url":"http://127.0.0.1:5200", "title":"Finished app", "steps":[
  {"action":"click", "selector":"text=Projects", "caption":"Project list"},
  {"action":"navigate", "url":"http://127.0.0.1:5200/results", "caption":"Results"},
  {"action":"scroll", "y":520, "caption":"Details"}
], "hold_s":3}
```

After finishing an app or website, use this tool with the actual running URL and a short journey covering the completed features. Poll `{"job_id":"…"}` rather than submitting the recording again. The result gives `state`, progress and a local `video_url` for the completed MP4. The tool reports connection and recording errors; no generated promo is substituted. The native tool, external MCP client and button share the same operation.

The configured Prospero connector supplies the server address and existing credentials. Faustus proxies the video with byte ranges so the player also works when the app runs on another machine. Prospero stores the video and contact sheet in its project library, with the actual page titles and visited URLs in the recipe. No GPU or text model is needed. Reading the summary or tool catalogue does not start a service.
