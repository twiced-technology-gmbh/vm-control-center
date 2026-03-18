// API client for VM Control Center

class ApiClient {
    constructor() {
        this.baseUrl = ''; // Relative URL to the API
        this.token = this.loadToken();
    }

    /**
     * Load the API token from the injected global variable
     */
    loadToken() {
        // The token is injected by the server in the HTML template
        return window.API_TOKEN || '';
    }

    /**
     * Make a GET request
     */
    async get(endpoint) {
        return this._fetch('GET', endpoint);
    }

    /**
     * Make a POST request
     */
    async post(endpoint, data) {
        return this._fetch('POST', endpoint, data);
    }

    /**
     * Make a PUT request
     */
    async put(endpoint, data) {
        return this._fetch('PUT', endpoint, data);
    }

    /**
     * Make a DELETE request
     */
    async delete(endpoint) {
        return this._fetch('DELETE', endpoint);
    }

    /**
     * Internal fetch method
     */
    async _fetch(method, endpoint, data = null) {
        const url = `${this.baseUrl}${endpoint}`;
        const headers = {
            'Content-Type': 'application/json',
            'X-Local-Token': this.token
        };

        const config = {
            method,
            headers,
            credentials: 'same-origin'
        };

        if (data) {
            config.body = JSON.stringify(data);
        }

        const response = await fetch(url, config);

        if (!response.ok) {
            const error = await response.json().catch(() => ({}));
            throw new Error(error.detail || 'An error occurred');
        }

        // Handle empty responses
        const contentType = response.headers.get('content-type');
        if (contentType && contentType.includes('application/json')) {
            return await response.json();
        }

        return await response.text();
    }
}

// Create a singleton instance
const api = new ApiClient();
