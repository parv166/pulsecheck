"use strict";

const form = document.getElementById("login-form");
const button = document.getElementById("login-button");
const errorBox = document.getElementById("login-error");

// Avoid showing the login form again when a valid session already exists.
fetch("/api/auth/session", {
    credentials: "same-origin"
}).then(response => {
    if (response.ok) {
        window.location.replace("/");
    }
}).catch(() => {
    // The form remains available if the session check fails.
});

form.addEventListener("submit", async event => {
    event.preventDefault();

    button.disabled = true;
    button.textContent = "Signing in...";
    errorBox.hidden = true;

    try {
        const response = await fetch("/api/auth/login", {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                username: document.getElementById("username").value.trim(),
                password: document.getElementById("password").value
            })
        });

        const data = await response.json().catch(() => ({}));

        if (!response.ok) {
            throw new Error(data.detail || "Unable to sign in.");
        }

        window.location.replace("/");
    } catch (error) {
        errorBox.textContent = error.message;
        errorBox.hidden = false;
    } finally {
        button.disabled = false;
        button.textContent = "Sign in";
    }
});
