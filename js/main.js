// Landing Authentication Interface Tab Coordinator
document.addEventListener("DOMContentLoaded", () => {
  const loginForm = document.getElementById("tab-login");
  const regForm = document.getElementById("tab-register");
  const tabSelects = document.querySelectorAll(".auth-tab");

  if (!loginForm || !regForm) return;

  tabSelects.forEach(btn => {
    btn.addEventListener("click", () => {
      tabSelects.forEach(b => b.classList.remove("active"));
      btn.classList.add("active");

      if (btn.dataset.tab === "login") {
        loginForm.classList.remove("hidden");
        regForm.classList.add("hidden");
      } else {
        regForm.classList.remove("hidden");
        loginForm.classList.add("hidden");
      }
    });
  });

  // Execute landing access authentication logins
  const primaryLoginBtn = document.getElementById("btn-login");
  if (primaryLoginBtn) {
    primaryLoginBtn.addEventListener("click", async () => {
      const u = document.getElementById("login-username").value;
      const p = document.getElementById("login-password").value;
      const errorMsg = document.getElementById("login-error");

      try {
        await Auth.login(u, p);
        window.location.href = "html/dashboard.html";
      } catch (err) {
        if (errorMsg) {
          errorMsg.textContent = err.message;
          errorMsg.classList.remove("hidden");
        }
      }
    });
  }

  // Process registration creations
  const completeRegBtn = document.getElementById("btn-register");
  if (completeRegBtn) {
    completeRegBtn.addEventListener("click", async () => {
      const u = document.getElementById("reg-username").value;
      const d = document.getElementById("reg-display").value;
      const p = document.getElementById("reg-password").value;
      const c = document.getElementById("reg-confirm").value;
      const errorMsg = document.getElementById("reg-error");

      if (p !== c) {
        if (errorMsg) {
          errorMsg.textContent = "Passwords do not match.";
          errorMsg.classList.remove("hidden");
        }
        return;
      }

      try {
        await Auth.register(u, d, p);
        window.location.href = "html/dashboard.html";
      } catch (err) {
        if (errorMsg) {
          errorMsg.textContent = err.message;
          errorMsg.classList.remove("hidden");
        }
      }
    });
  }
});