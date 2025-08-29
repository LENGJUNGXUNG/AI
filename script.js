const pdfFile = document.getElementById("pdfFile");
const extractButton = document.getElementById("extractButton");
const downloadButton = document.getElementById("downloadButton");
const messageDiv = document.getElementById("message");
const loadingSpinner = document.getElementById("loadingSpinner");
const contentPreview = document.getElementById("contentPreview");
const previewTitle = document.getElementById("previewTitle");
const initialMessage = document.getElementById("initialMessage");

let extractedData = [];

/**
 * Event listener for the Extract Content button.
 * Fetches data from the server and displays it.
 */
extractButton.addEventListener("click", async (event) => {
  event.preventDefault();

  const files = pdfFile.files;
  if (files.length === 0) {
    setMessage("Please select one or more PDF files first.", "yellow");
    return;
  }

  setMessage(`Processing ${files.length} PDF file(s)...`, "blue");
  setLoading(true);
  downloadButton.classList.add("hidden");
  clearContentPreview();

  const formData = new FormData();
  for (const file of files) {
    formData.append("pdfFile", file);
  }

  try {
    const response = await fetch("http://127.0.0.1:5000/upload-pdfs", {
      method: "POST",
      body: formData,
    });

    if (!response.ok) {
      const errorData = await response.json();
      throw new Error(
        errorData.error || `HTTP error! status: ${response.status}`
      );
    }

    const result = await response.json();
    extractedData = result.extracted_data;

    if (extractedData.length > 0) {
      displayContent(extractedData);
      setMessage(
        "Content extracted successfully! You can now download the PDF.",
        "green"
      );

      const generateResponse = await fetch(
        "http://127.0.0.1:5000/generate-pdf",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ extracted_data: extractedData }),
        }
      );

      downloadButton.classList.remove("hidden");
    } else {
      setMessage("No content could be extracted from the files.", "yellow");
      downloadButton.classList.add("hidden");
    }
  } catch (error) {
    console.error("Error:", error);
    setMessage(
      `An error occurred: ${error.message}. Please check the server console for details.`,
      "red"
    );
  } finally {
    setLoading(false);
  }
});

/**
 * Event listener for the Download button.
 * Triggers a file download.
 */
downloadButton.addEventListener("click", async () => {
  if (extractedData.length === 0) {
    setMessage("No content to download. Please extract first.", "yellow");
    return;
  }

  setMessage("Generating PDF for download...", "blue");
  setLoading(true);

  try {
    const response = await fetch("http://127.0.0.1:5000/generate-pdf", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ extracted_data: extractedData }),
    });

    if (!response.ok) {
      const errorData = await response.json();
      throw new Error(
        errorData.error || `HTTP error! status: ${response.status}`
      );
    }

    const blob = await response.blob();
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.style.display = "none";
    a.href = url;
    a.download = "extracted_content.pdf";
    document.body.appendChild(a);
    a.click();
    window.URL.revokeObjectURL(url);

    setMessage(`Download started for extracted_content.pdf.`, "green");
  } catch (error) {
    console.error("Error:", error);
    setMessage(`An error occurred during download: ${error.message}.`, "red");
  } finally {
    setLoading(false);
  }
});

/**
 * Sets the message text and color.
 * @param {string} text - The message to display.
 * @param {string} type - The color type ('blue', 'green', 'yellow', 'red').
 */
function setMessage(text, type) {
  messageDiv.textContent = text;
  messageDiv.className = `text-center text-lg mb-4 font-semibold`;
  if (type === "blue") messageDiv.classList.add("text-blue-700");
  if (type === "green") messageDiv.classList.add("text-green-600");
  if (type === "yellow") messageDiv.classList.add("text-yellow-700");
  if (type === "red") messageDiv.classList.add("text-red-600");
}

/**
 * Toggles the loading state of the UI.
 * @param {boolean} isLoading - True to show spinner and disable buttons, false otherwise.
 */
function setLoading(isLoading) {
  extractButton.disabled = isLoading;
  downloadButton.disabled = isLoading;
  pdfFile.disabled = isLoading;
  if (isLoading) {
    loadingSpinner.classList.remove("hidden");
  } else {
    loadingSpinner.classList.add("hidden");
  }
}

/**
 * Clears the content preview area and shows the initial message.
 */
function clearContentPreview() {
  previewTitle.classList.add("hidden");
  contentPreview.innerHTML = "";
  const initialMsgP = document.createElement("p");
  initialMsgP.className = "text-gray-500 text-center";
  initialMsgP.id = "initialMessage";
  initialMsgP.textContent = "Extracted content will appear here.";
  contentPreview.appendChild(initialMsgP);
}

/**
 * Displays the extracted content (text, images, tables).
 * @param {Array} data - The extracted data to display.
 */
function displayContent(data) {
  clearContentPreview();
  previewTitle.classList.remove("hidden");
  const initialMsg = document.getElementById("initialMessage");
  if (initialMsg) {
    initialMsg.classList.add("hidden");
  }

  let currentSource = "";
  data.forEach((item) => {
    if (currentSource !== item.source_filename) {
      currentSource = item.source_filename;
      const header = document.createElement("h3");
      header.className = "text-xl font-bold text-gray-700 mt-6 mb-2";
      header.textContent = `--- Content from: ${currentSource} ---`;
      contentPreview.appendChild(header);
    }

    if (item.type === "text") {
      const p = document.createElement("p");
      p.className = "mb-2 text-gray-800";
      p.textContent = item.content;
      contentPreview.appendChild(p);
    } else if (item.type === "image") {
      const imgContainer = document.createElement("div");
      imgContainer.className = "my-4 text-center";
      const imgTitle = document.createElement("h4");
      imgTitle.className = "text-md font-semibold text-gray-700";
      imgTitle.textContent = item.title;
      const img = document.createElement("img");
      img.src = item.content;
      img.alt = item.title;
      img.className = "max-w-full h-auto mx-auto rounded-lg shadow-md mt-2";
      imgContainer.appendChild(imgTitle);
      imgContainer.appendChild(img);
      contentPreview.appendChild(imgContainer);
    } else if (item.type === "table") {
      const tableContainer = document.createElement("div");
      tableContainer.className = "my-4 overflow-x-auto";
      const tableTitle = document.createElement("h4");
      tableTitle.className =
        "text-md font-semibold text-gray-700 text-center mb-2";
      tableTitle.textContent = item.title;

      const tableData = JSON.parse(item.content);
      const tableElement = document.createElement("table");
      tableElement.className =
        "w-full text-sm text-left text-gray-500 rounded-lg shadow-md overflow-hidden";

      const thead = document.createElement("thead");
      thead.className = "text-xs text-gray-700 uppercase bg-gray-200";
      const headerRow = document.createElement("tr");
      tableData.columns.forEach((col) => {
        const th = document.createElement("th");
        th.scope = "col";
        th.className = "px-6 py-3";
        th.textContent = col;
        headerRow.appendChild(th);
      });
      thead.appendChild(headerRow);
      tableElement.appendChild(thead);

      const tbody = document.createElement("tbody");
      tbody.className = "bg-white divide-y divide-gray-200";
      tableData.data.forEach((row) => {
        const tr = document.createElement("tr");
        tr.className = "hover:bg-gray-100";
        row.forEach((cell) => {
          const td = document.createElement("td");
          td.className = "px-6 py-4";
          td.textContent = cell;
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      });
      tableElement.appendChild(tbody);

      tableContainer.appendChild(tableTitle);
      tableContainer.appendChild(tableElement);
      contentPreview.appendChild(tableContainer);
    }
  });
}
