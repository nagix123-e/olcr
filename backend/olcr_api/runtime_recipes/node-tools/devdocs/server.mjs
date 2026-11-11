#!/usr/bin/env node
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
// Uses Node 20 native fetch.
const DEVDOCS_BASE_URL = "https://devdocs.io";
const DEVDOCS_DOCUMENTS_URL = "https://documents.devdocs.io";
// Types
// Cache
const cache = new Map                                          ();
const CACHE_TTL = 1000 * 60 * 60; // 1 hour
function getCached   (key        )           {
  const cached = cache.get(key);
  if (cached && Date.now() - cached.timestamp < CACHE_TTL) {
    return cached.data     ;
  }
  cache.delete(key);
  return null;
}
function setCache(key        , data     )       {
  cache.set(key, { data, timestamp: Date.now() });
}
// API functions
async function fetchDocsList()                         {
  const cached = getCached               ("docs_list");
  if (cached) return cached;
  const response = await fetch(`${DEVDOCS_BASE_URL}/docs.json`);
  if (!response.ok) {
    throw new Error(`Failed to fetch docs list: ${response.statusText}`);
  }
  const docs = (await response.json())                 ;
  setCache("docs_list", docs);
  return docs;
}
async function fetchDocIndex(slug        )                        {
  const cached = getCached              (`index_${slug}`);
  if (cached) return cached;
  const response = await fetch(`${DEVDOCS_DOCUMENTS_URL}/${slug}/index.json`);
  if (!response.ok) {
    throw new Error(`Failed to fetch index for ${slug}: ${response.statusText}`);
  }
  const rawIndex = (await response.json())       ;
  // Parse the index entries - they have name, path, and type fields
  const entries               = rawIndex.entries || [];
  setCache(`index_${slug}`, entries);
  return entries;
}
async function fetchDocContent(slug        , path        )                  {
  const cacheKey = `content_${slug}_${path}`;
  const cached = getCached        (cacheKey);
  if (cached) return cached;
  const response = await fetch(`${DEVDOCS_DOCUMENTS_URL}/${slug}/${path}.html`);
  if (!response.ok) {
    throw new Error(`Failed to fetch content: ${response.statusText}`);
  }
  const content = await response.text();
  setCache(cacheKey, content);
  return content;
}
async function searchInDoc(slug        , query        )                          {
  const index = await fetchDocIndex(slug);
  const lowerQuery = query.toLowerCase();
  return index
    .filter(entry => entry.name.toLowerCase().includes(lowerQuery))
    .map(entry => ({
      doc: slug,
      name: entry.name,
      path: entry.path || entry.name.toLowerCase().replace(/\s+/g, '-'),
      type: entry.type,
      url: `${DEVDOCS_BASE_URL}/${slug}/${entry.path || entry.name.toLowerCase().replace(/\s+/g, '-')}`,
    }))
    .slice(0, 20); // Limit to 20 results
}
async function searchAllDocs(query        , docFilter           )                          {
  const docs = await fetchDocsList();
  const docsToSearch = docFilter && docFilter.length > 0
    ? docs.filter(doc => docFilter.includes(doc.slug))
    : docs.slice(0, 20); // Limit to first 20 docs if no filter
  const results                 = [];
  for (const doc of docsToSearch) {
    try {
      const docResults = await searchInDoc(doc.slug, query);
      results.push(...docResults);
      // Limit total results
      if (results.length >= 50) {
        break;
      }
    } catch (error) {
      // Skip docs that fail to load
      console.error(`Failed to search in ${doc.slug}:`, error);
    }
  }
  return results.slice(0, 50);
}
// MCP Server
const server = new Server(
  {
    name: "devdocs-mcp-server",
    version: "1.0.0",
  },
  {
    capabilities: {
      tools: {},
    },
  }
);
// Define tools
const tools         = [
  {
    name: "list_docs",
    description: "List all available documentation sets on DevDocs.io. Returns name, slug, version, and other metadata for each documentation.",
    inputSchema: {
      type: "object",
      properties: {
        filter: {
          type: "string",
          description: "Optional filter string to search for specific documentation by name",
        },
      },
    },
  },
  {
    name: "search_doc",
    description: "Search within a specific documentation set for entries matching the query. Returns matching entries with their paths and types.",
    inputSchema: {
      type: "object",
      properties: {
        doc_slug: {
          type: "string",
          description: "The slug of the documentation to search (e.g., 'javascript', 'python~3.12', 'react')",
        },
        query: {
          type: "string",
          description: "The search query to find matching entries",
        },
      },
      required: ["doc_slug", "query"],
    },
  },
  {
    name: "get_doc_content",
    description: "Fetch the full HTML content for a specific documentation entry. Use the path from search results.",
    inputSchema: {
      type: "object",
      properties: {
        doc_slug: {
          type: "string",
          description: "The slug of the documentation (e.g., 'javascript', 'python~3.12')",
        },
        path: {
          type: "string",
          description: "The path to the specific entry (from search results)",
        },
      },
      required: ["doc_slug", "path"],
    },
  },
  {
    name: "search_all_docs",
    description: "Search across multiple documentation sets. Optionally filter by specific documentation slugs.",
    inputSchema: {
      type: "object",
      properties: {
        query: {
          type: "string",
          description: "The search query to find matching entries",
        },
        doc_slugs: {
          type: "array",
          items: { type: "string" },
          description: "Optional array of documentation slugs to search within. If not provided, searches across top documentation sets.",
        },
      },
      required: ["query"],
    },
  },
  {
    name: "get_doc_index",
    description: "Get the complete index of entries for a specific documentation set. Useful for browsing available topics.",
    inputSchema: {
      type: "object",
      properties: {
        doc_slug: {
          type: "string",
          description: "The slug of the documentation (e.g., 'javascript', 'python~3.12')",
        },
      },
      required: ["doc_slug"],
    },
  },
];
// Handle list tools request
server.setRequestHandler(ListToolsRequestSchema, async () => {
  return { tools };
});
// Handle tool calls
server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;
  try {
    switch (name) {
      case "list_docs": {
        const docs = await fetchDocsList();
        let filteredDocs = docs;
        if (args?.filter && typeof args.filter === "string") {
          const filter = args.filter.toLowerCase();
          filteredDocs = docs.filter(
            doc => doc.name.toLowerCase().includes(filter) ||
                   doc.slug.toLowerCase().includes(filter)
          );
        }
        return {
          content: [
            {
              type: "text",
              text: JSON.stringify(
                filteredDocs.map(doc => ({
                  name: doc.name,
                  slug: doc.slug,
                  version: doc.version,
                  release: doc.release,
                  type: doc.type,
                  links: doc.links,
                  mtime: new Date(doc.mtime * 1000).toISOString(),
                  size_kb: Math.round(doc.db_size / 1024),
                })),
                null,
                2
              ),
            },
          ],
        };
      }
      case "search_doc": {
        const docSlug = args?.doc_slug          ;
        const query = args?.query          ;
        if (!docSlug || !query) {
          throw new Error("doc_slug and query are required");
        }
        const results = await searchInDoc(docSlug, query);
        return {
          content: [
            {
              type: "text",
              text: JSON.stringify(results, null, 2),
            },
          ],
        };
      }
      case "get_doc_content": {
        const docSlug = args?.doc_slug          ;
        const path = args?.path          ;
        if (!docSlug || !path) {
          throw new Error("doc_slug and path are required");
        }
        const content = await fetchDocContent(docSlug, path);
        return {
          content: [
            {
              type: "text",
              text: content,
            },
          ],
        };
      }
      case "search_all_docs": {
        const query = args?.query          ;
        const docSlugs = args?.doc_slugs                        ;
        if (!query) {
          throw new Error("query is required");
        }
        const results = await searchAllDocs(query, docSlugs);
        return {
          content: [
            {
              type: "text",
              text: JSON.stringify(results, null, 2),
            },
          ],
        };
      }
      case "get_doc_index": {
        const docSlug = args?.doc_slug          ;
        if (!docSlug) {
          throw new Error("doc_slug is required");
        }
        const index = await fetchDocIndex(docSlug);
        return {
          content: [
            {
              type: "text",
              text: JSON.stringify(
                {
                  doc: docSlug,
                  total_entries: index.length,
                  entries: index.slice(0, 100), // Return first 100 entries
                  truncated: index.length > 100,
                },
                null,
                2
              ),
            },
          ],
        };
      }
      default:
        throw new Error(`Unknown tool: ${name}`);
    }
  } catch (error) {
    const errorMessage = error instanceof Error ? error.message : String(error);
    return {
      content: [
        {
          type: "text",
          text: `Error: ${errorMessage}`,
        },
      ],
      isError: true,
    };
  }
});
// Start server
async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("DevDocs MCP Server running on stdio");
}
main().catch((error) => {
  console.error("Fatal error:", error);
  process.exit(1);
});
