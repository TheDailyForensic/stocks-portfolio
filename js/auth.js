/**
 * STOCKSIM -- LOCAL AUTHENTICATION MANAGER
 * Direct identity mapping logic using localized encryption parameters
 */

const Auth = {
  getUsers() {
    return JSON.parse(localStorage.getItem("stocksim_users") || "{}");
  },

  getActiveUser() {
    const session = localStorage.getItem("stocksim_session");
    if (!session) return null;
    return this.getUsers()[session] || null;
  },

  async hashPassword(password) {
    const msgBuffer = new TextEncoder().encode(password);
    const hashBuffer = await crypto.subtle.digest("SHA-256", msgBuffer);
    return Array.from(new Uint8Array(hashBuffer)).map(b => b.toString(16).padStart(2, "0")).join("");
  },

  async register(username, displayName, password) {
    const users = this.getUsers();
    const handle = username.toLowerCase().trim();

    if (!handle || !displayName || password.length < 8) {
      throw new Error("Invalid entries. Passwords must be at least 8 characters.");
    }
    if (users[handle]) {
      throw new Error("Trader handle has already been registered on this node.");
    }

    users[handle] = {
      username: handle,
      displayName: displayName.trim(),
      password: await this.hashPassword(password),
      cash: 10000.00,
      holdings: {},
      history: [],
      watchlist: ["AAPL", "TSLA", "NVDA"],
      equityHistory: [10000.00],
      datesHistory: [new Date().toLocaleDateString()]
    };

    localStorage.setItem("stocksim_users", JSON.stringify(users));
    localStorage.setItem("stocksim_session", handle);
    return users[handle];
  },

  async login(username, password) {
    const users = this.getUsers();
    const handle = username.toLowerCase().trim();
    const user = users[handle];

    if (!user || user.password !== await this.hashPassword(password)) {
      throw new Error("Security verification handshake failed.");
    }

    localStorage.setItem("stocksim_session", handle);
    return user;
  },

  logout() {
    localStorage.removeItem("stocksim_session");
    // Relative exit pathway directly to root landing page asset
    window.location.href = "./../index.html";
  },

  saveUserData(user) {
    const users = this.getUsers();
    users[user.username] = user;
    localStorage.setItem("stocksim_users", JSON.stringify(users));
  },

  requireAuth() {
    const user = this.getActiveUser();
    if (!user) {
      const path = window.location.pathname;
      if (!path.endsWith("index.html") && path !== "/") {
        window.location.href = "./../index.html";
      }
    }
    return user;
  }
};

// Global Initialization Hooks
document.addEventListener("DOMContentLoaded", () => {
  const user = Auth.getActiveUser();
  const path = window.location.pathname;
  // Dynamic validation matching current local server context routing
  const isIndex = path.endsWith("index.html") || path.endsWith("/");

  if (!user && !isIndex) {
    // Relative fallback redirection execution
    window.location.href = "./../index.html";
  } else if (user && isIndex) {
    // Relative dashboard entry acceleration path
    window.location.href = "html/dashboard.html";
  }

  const logoutBtn = document.getElementById("btn-logout");
  if (logoutBtn) logoutBtn.addEventListener("click", () => Auth.logout());

  if (user) {
    const avatar = document.getElementById("user-avatar");
    const display = document.getElementById("user-display");
    const handle = document.getElementById("user-handle");
    const mobileCash = document.getElementById("topbar-cash");

    if (avatar) avatar.textContent = user.displayName.charAt(0).toUpperCase();
    if (display) display.textContent = user.displayName;
    if (handle) handle.textContent = "@" + user.username;
    if (mobileCash) mobileCash.textContent = "$" + user.cash.toLocaleString(undefined, { minimumFractionDigits: 2 });
  }

  // Sidebar toggle menu processing for mobile environments
  const burger = document.getElementById("burger");
  const sidebar = document.getElementById("sidebar");
  if (burger && sidebar) {
    burger.addEventListener("click", () => sidebar.classList.toggle("open"));
  }
});
