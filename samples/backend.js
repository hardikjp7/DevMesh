const prom = new Promise((resolve, reject) => {
    setTimeout(() => {
        resolve("Resolved prom one");
    }, 2000);
});

prom.then((result) => {
    console.log(result);
});


const prom2 = new Promise((resolve, reject) => {
    setTimeout(() => {
        let error = true;
        if (error) resolve({user: "hitesh", password: "123"});
        else reject("Something went wrong");
    }, 2000);
});

prom2.then((result) => {
    console.log(result.user);
    return result.user;
})
// .then((user) => {
//     console.log(user);
// })
// .then(() => {
//  console.log(user);
// })
.catch((error) => {
    console.log(error);
})