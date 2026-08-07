document.addEventListener("htmx:afterSwap", (event) => {
  if (event.detail.target.id === "main-content") {
    event.detail.target.focus();
  }
});
