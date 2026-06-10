// Header button: downloads the Partner API OpenAPI spec, which imports straight
// into Postman (File -> Import -> drop the file -> full collection). Static file
// served from public/. Rendered in the header via the `head-navigation-start` slot.
export function PostmanDownload() {
  return (
    <a
      href="/docs/tatvacare-partner-api.openapi.json"
      download="tatvacare-partner-api.openapi.json"
      title="Download the OpenAPI spec — import it into Postman to get the full collection"
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        fontSize: 14,
        fontWeight: 500,
        textDecoration: "none",
        padding: "4px 8px",
        borderRadius: 6,
      }}
    >
      <img src="/docs/postman-icon.svg" alt="Postman" width={16} height={16} />
      Download for Postman
    </a>
  );
}

export default PostmanDownload;
