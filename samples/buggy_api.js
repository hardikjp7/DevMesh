// Sample buggy file: unhandled promise rejection.
// Use this to test that the pipeline flags WARNING-level issues correctly.

function fetchUserData(userId) {
  // BUG: no .catch(), and no try/catch around the await elsewhere in the app
  fetch(`https://api.example.com/users/${userId}`)
    .then((response) => response.json())
    .then((data) => {
      console.log(data);
    });
}

async function loadDashboard(userId) {
  // BUG: await without try/catch — unhandled rejection will crash the process
  const data = await fetchUserData(userId);
  return data;
}

module.exports = { fetchUserData, loadDashboard };
