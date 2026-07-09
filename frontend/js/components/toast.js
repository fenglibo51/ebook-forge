/* ==================== Toast 通知 ==================== */
const Toast = {
  _container: null,

  _ensureContainer() {
    if (!this._container) {
      this._container = document.getElementById("toast-container");
    }
  },

  show(message, type = "info", duration = 3000) {
    this._ensureContainer();
    const el = document.createElement("div");
    el.className = `toast toast-${type}`;
    el.textContent = message;
    this._container.appendChild(el);
    setTimeout(() => {
      el.style.opacity = "0";
      el.style.transition = "opacity 0.3s";
      setTimeout(() => el.remove(), 300);
    }, duration);
  },

  success(msg, duration) { this.show(msg, "success", duration); },
  error(msg, duration) { this.show(msg, "error", duration); },
  info(msg, duration) { this.show(msg, "info", duration); },
};
