async function request(path, options) {
  let res;
  try {
    res = await fetch(path, options);
  } catch {
    throw new Error("Could not reach the server.");
  }
  if (!res.ok) {
    let message = `Request failed (HTTP ${res.status})`;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") message = body.detail;
      else if (Array.isArray(body.detail)) message = body.detail.map((d) => d.msg).join("; ");
    } catch {
      /* keep default message */
    }
    throw new Error(message);
  }
  return res.status === 204 ? null : res.json();
}

const post = (path, body) =>
  request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

export const searchPapers = (q) => request(`/api/search?q=${encodeURIComponent(q)}`);
export const listPapers = () => request("/api/papers");
export const getPaper = (id) => request(`/api/papers/${id}`);
export const deletePaper = (id) => request(`/api/papers/${id}`, { method: "DELETE" });
export const savePaper = (result) => post("/api/papers", result);
export const uploadPaper = (file) => {
  const form = new FormData();
  form.append("file", file);
  return request("/api/papers/upload", { method: "POST", body: form });
};

// target is { paper_id } for a saved paper or { paper } for an unsaved search result
export const summarize = (target) => post("/api/summarize", target);
export const ask = (target, question) => post("/api/ask", { ...target, question });
