const form = document.querySelector("#upload-form");
const videoInput = document.querySelector("#video");
const fileLabel = document.querySelector("#file-label");
const progressBox = document.querySelector("#progress");
const progressState = document.querySelector("#progress-state");
const progressMessage = document.querySelector("#progress-message");
const barFill = document.querySelector("#bar-fill");
const resultBox = document.querySelector("#result");
const fileList = document.querySelector("#file-list");
const downloadLink = document.querySelector("#download-link");
const serverStatus = document.querySelector("#server-status");

let pollTimer = null;

videoInput.addEventListener("change", () => {
  fileLabel.textContent = videoInput.files[0]?.name || "Choose video file";
});

function setProgress(state, message) {
  progressBox.hidden = false;
  progressState.textContent = state;
  progressMessage.textContent = message;
  serverStatus.textContent = state;

  if (state === "Queued") barFill.style.width = "25%";
  if (state === "Running") barFill.style.width = "65%";
  if (state === "Complete") barFill.style.width = "100%";
  if (state === "Failed") {
    barFill.style.width = "100%";
    progressMessage.classList.add("error");
  } else {
    progressMessage.classList.remove("error");
  }
}

async function pollJob(jobId) {
  const response = await fetch(`/api/jobs/${jobId}`);
  const job = await response.json();
  const stateLabel = job.state.charAt(0).toUpperCase() + job.state.slice(1);
  setProgress(stateLabel, job.message);

  if (job.state === "complete") {
    clearInterval(pollTimer);
    fileList.innerHTML = "";
    for (const file of job.files || []) {
      const item = document.createElement("li");
      item.textContent = file;
      fileList.appendChild(item);
    }
    downloadLink.href = job.download_url;
    resultBox.hidden = false;
    form.querySelector("button").disabled = false;
  }

  if (job.state === "failed") {
    clearInterval(pollTimer);
    form.querySelector("button").disabled = false;
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  resultBox.hidden = true;
  form.querySelector("button").disabled = true;
  setProgress("Queued", "Uploading video...");

  const formData = new FormData(form);
  const response = await fetch("/api/jobs", {
    method: "POST",
    body: formData,
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: "Upload failed." }));
    setProgress("Failed", error.detail || "Upload failed.");
    form.querySelector("button").disabled = false;
    return;
  }

  const data = await response.json();
  setProgress("Queued", "Upload received. Waiting to start...");
  pollTimer = setInterval(() => pollJob(data.job_id), 3000);
  pollJob(data.job_id);
});
