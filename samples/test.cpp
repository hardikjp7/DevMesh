#include <iostream>
using namespace std;
 
class Employee {
    private:
    double salary;
    protected:
    string department;
    public:
    string name;

    void setSalary(double salary) {
        this->salary = salary;
    }
    double getSalary() {
        cout << "Salary: " << salary << endl;
        return salary;
    }
};


int main()
 
{
 Employee e1;
    e1.name = "Vatsal";
    e1.setSalary(50000);
    cout << e1.name << " earns " << e1.getSalary() << endl;

 return 0;
}