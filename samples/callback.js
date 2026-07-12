function greet(name, callback) {
    console.log("Hello " + name);
    callback();
  }
  greet("Vatsal", () => console.log("Greeting done!"));
  